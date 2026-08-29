"""
src/orchestrator.py

Wires the deterministic State Extractor, Sandbox Verifier, and Data
Loss Guardian together with the LLM-calling Migration Synthesizer into
the actual MigraLoop self-correction loop:

    extract drift -> synthesize migration -> apply in a FRESH sandbox
        -> Guardian check
        -> on failure (SQL error, data loss, or a broken table), feed
           the error back to the Synthesizer and retry from a clean
           copy of the physical state, up to MAX_ATTEMPTS
        -> on success, done

Design notes
------------
Retry logic lives here as a plain loop, not as a separate "Constraint
Resolver" agent class. Resolving a failure IS just "call the
Synthesizer again with the error attached" -- the Synthesizer already
accepts that (prior_attempt_sql / prior_error / prior_error_type).

Each attempt gets a brand-new sandbox re-provisioned from the original
physical.db, rather than reusing one sandbox across retries. A failed
attempt's SQL is still committed to the sandbox by the time the
Guardian catches it (apply_migration only rolls back on a SQL-level
error, not on a Guardian-detected data-loss failure that executed
cleanly) -- so retrying against the same sandbox means the second
attempt is no longer starting from the real physical state, it's
starting from the wreckage of the first attempt. That compounds
failures instead of correcting them. Re-provisioning per attempt costs
a cheap file copy and guarantees every attempt reasons about the same
starting point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.agents.extractor import StateExtractor
from src.agents.synthesizer import MigrationSynthesizer, SynthesisResult
from src.core.guardian import DataLossGuardian, DatabaseSnapshot
from src.core.sandbox import SandboxVerifier
from src.core.trajectory import TrajectoryLogger

MAX_ATTEMPTS = 3


@dataclass
class LoopResult:
    case_name: str
    outcome: str  # "success" | "failed_max_retries" | "no_drift"
    attempts: int
    final_migration_sql: Optional[str] = None
    trajectory_path: Optional[str] = None
    attempt_errors: list = field(default_factory=list)  # errors from failed attempts, for evaluate.py


def run_case(case_dir: Path, synthesizer: Optional[MigrationSynthesizer] = None) -> LoopResult:
    """Run the full MigraLoop advanced loop against one benchmark case
    directory (must contain physical.db and target_schema.sql)."""
    case_name = case_dir.name
    physical_db_path = str(case_dir / "physical.db")
    target_schema_sql = (case_dir / "target_schema.sql").read_text()

    synthesizer = synthesizer or MigrationSynthesizer()
    logger = TrajectoryLogger(case_name=case_name, output_dir="trajectories/advanced")
    guardian = DataLossGuardian()

    drift_report, pre_snapshot = _probe_physical_state(case_name, physical_db_path, target_schema_sql, guardian)
    logger.log_step(
        agent="state_extractor",
        action="extract_drift",
        output_data={"has_drift": drift_report.has_drift, "summary": drift_report.to_prompt_text()},
    )

    if not drift_report.has_drift:
        logger.finalize(outcome="no_drift")
        path = logger.write()
        return LoopResult(case_name=case_name, outcome="no_drift", attempts=0, trajectory_path=path)

    prompt_text = drift_report.to_prompt_text() + "\n\nFull target schema SQL:\n" + target_schema_sql

    prior_sql: Optional[str] = None
    prior_error: Optional[str] = None
    prior_error_type: Optional[str] = None
    attempt_errors: list = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.log_tool_call(
            agent="migration_synthesizer",
            tool="synthesize",
            input_data={"attempt": attempt, "prior_error": prior_error},
        )
        result: SynthesisResult = synthesizer.synthesize(
            drift_report_text=prompt_text,
            prior_attempt_sql=prior_sql,
            prior_error=prior_error,
            prior_error_type=prior_error_type,
        )
        logger.log_environment_response(
            agent="migration_synthesizer",
            action="synthesize_result",
            output_data={"migration_sql": result.migration_sql, "reasoning": result.reasoning},
        )

        # Fresh sandbox from the ORIGINAL physical state every attempt --
        # see module docstring for why this can't be reused across retries.
        sb = SandboxVerifier(case_name)
        sb.provision(source_db_path=physical_db_path)
        try:
            migration_result = sb.apply_migration(result.migration_sql)
            logger.log_tool_call(
                agent="sandbox_verifier",
                tool="apply_migration",
                output_data={"success": migration_result.success, "error": migration_result.error},
            )

            if not migration_result.success:
                error_message = migration_result.error or "unknown SQL error"
                error_type = migration_result.error_type or "SQLError"
                logger.log_error(agent="sandbox_verifier", error_type=error_type, message=error_message)
                attempt_errors.append(f"attempt {attempt} [{error_type}]: {error_message}")
                prior_sql, prior_error, prior_error_type = result.migration_sql, error_message, error_type
                if attempt < MAX_ATTEMPTS:
                    logger.log_self_correction(
                        agent="orchestrator",
                        prior_error=error_message,
                        correction_strategy="retry with SQL error fed back to synthesizer",
                        new_input={},
                    )
                continue

            post_snapshot = guardian.snapshot(sb.connection)
            loss_report = guardian.compare(pre_snapshot, post_snapshot, manifest=result.manifest)

            if not loss_report.passed:
                logger.log_error(
                    agent="data_loss_guardian", error_type="DataLossDetected", message=loss_report.details
                )
                attempt_errors.append(f"attempt {attempt} [DataLossDetected]: {loss_report.details}")
                prior_sql, prior_error, prior_error_type = result.migration_sql, loss_report.details, "DataLossDetected"
                if attempt < MAX_ATTEMPTS:
                    logger.log_self_correction(
                        agent="orchestrator",
                        prior_error=loss_report.details,
                        correction_strategy="retry with data-loss report fed back to synthesizer",
                        new_input={},
                    )
                continue

            broken_table = _first_broken_table(sb)
            if broken_table is not None:
                message = f"table '{broken_table}' not queryable after migration"
                logger.log_error(agent="sandbox_verifier", error_type="IntegrationCheckFailed", message=message)
                attempt_errors.append(f"attempt {attempt} [IntegrationCheckFailed]: {message}")
                prior_sql, prior_error, prior_error_type = result.migration_sql, message, "IntegrationCheckFailed"
                if attempt < MAX_ATTEMPTS:
                    logger.log_self_correction(
                        agent="orchestrator",
                        prior_error=message,
                        correction_strategy="retry with integration-check failure fed back to synthesizer",
                        new_input={},
                    )
                continue

            logger.finalize(outcome="success")
            path = logger.write()
            return LoopResult(
                case_name=case_name,
                outcome="success",
                attempts=attempt,
                final_migration_sql=result.migration_sql,
                trajectory_path=path,
                attempt_errors=attempt_errors,
            )
        finally:
            sb.teardown()

    logger.finalize(outcome="failed_max_retries")
    path = logger.write()
    return LoopResult(
        case_name=case_name,
        outcome="failed_max_retries",
        attempts=MAX_ATTEMPTS,
        trajectory_path=path,
        attempt_errors=attempt_errors,
    )


def _probe_physical_state(case_name: str, physical_db_path: str, target_schema_sql: str, guardian: DataLossGuardian):
    """One throwaway sandbox used only to compute the drift report and
    the pre-migration content snapshot -- both are identical on every
    attempt since they only depend on the unchanging physical state, so
    there's no need to recompute them inside the retry loop."""
    probe = SandboxVerifier(f"{case_name}_probe")
    probe.provision(source_db_path=physical_db_path)
    try:
        drift_report = StateExtractor().extract_drift(probe.connection, target_schema_sql)
        pre_snapshot: DatabaseSnapshot = guardian.snapshot(probe.connection)
        return drift_report, pre_snapshot
    finally:
        probe.teardown()


def _first_broken_table(sb: SandboxVerifier) -> Optional[str]:
    for table_name in sb.list_tables():
        if not sb.run_integration_check(table_name).success:
            return table_name
    return None
