"""
src/baseline.py

The mandatory "simple baseline": a single zero-shot LLM call asked to
fix schema drift, with none of MigraLoop's safety machinery -- no
system-prompt rules about backfill patterns, no manifest requirement,
no sandbox retry loop, no Data Loss Guardian gate before "success" is
declared. This is deliberately naive: the whole point of Measured
Improvement is comparing against what an engineer gets by pasting the
problem into a chat model once, not against a strawman.

The baseline's SQL is still applied to a sandbox and checked by the
Guardian -- but only to MEASURE whether it lost data, never to retry
or block. A baseline "success" here means "the SQL executed without an
error", which is exactly the trap MigraLoop exists to catch: it says
nothing about whether the data survived.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic

from src.core.guardian import DataLossGuardian, MigrationManifest
from src.core.sandbox import SandboxVerifier
from src.core.trajectory import TrajectoryLogger

DEFAULT_MODEL = os.environ.get("MIGRALOOP_MODEL", "claude-sonnet-5")

_WRITE_MIGRATION_TOOL = {
    "name": "write_migration",
    "description": "Write a SQL migration.",
    "input_schema": {
        "type": "object",
        "properties": {
            "migration_sql": {
                "type": "string",
                "description": "SQLite SQL statements to migrate the current schema to the target schema.",
            },
        },
        "required": ["migration_sql"],
    },
}

# Deliberately minimal -- no backfill guidance, no drop-and-recreate
# warning, no mention of the manifest concept. This is what a
# developer gets from one naive prompt, not what MigraLoop's engineered
# system prompt (src/agents/synthesizer.py) produces.
BASELINE_SYSTEM_PROMPT = (
    "You are a database engineer. Given a current SQLite schema and a "
    "target schema, write a migration to transform the current schema "
    "into the target schema. Use the write_migration tool."
)


@dataclass
class BaselineResult:
    case_name: str
    migration_sql: Optional[str]
    sql_succeeded: bool
    sql_error: Optional[str]
    data_loss_detected: bool
    data_loss_details: str
    trajectory_path: Optional[str] = None


def run_baseline_case(case_dir: Path, client: Optional["anthropic.Anthropic"] = None) -> BaselineResult:
    case_name = case_dir.name
    physical_db_path = str(case_dir / "physical.db")
    target_schema_sql = (case_dir / "target_schema.sql").read_text()

    client = client or anthropic.Anthropic(default_headers={"accept-encoding": "gzip, deflate"})
    logger = TrajectoryLogger(case_name=case_name, output_dir="trajectories/baseline")

    sb = SandboxVerifier(case_name)
    sb.provision(source_db_path=physical_db_path)
    try:
        physical_schema_sql = "\n".join(
            r[0]
            for r in sb.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL;"
            ).fetchall()
        )

        user_message = (
            f"Current schema:\n{physical_schema_sql}\n\n"
            f"Target schema:\n{target_schema_sql}\n\n"
            "Write the migration."
        )
        logger.log_tool_call(agent="baseline_llm", tool="write_migration", input_data={"prompt": user_message})

        response = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=800,
            system=BASELINE_SYSTEM_PROMPT,
            tools=[_WRITE_MIGRATION_TOOL],
            tool_choice={"type": "tool", "name": "write_migration"},
            messages=[{"role": "user", "content": user_message}],
        )
        tool_use_block = next(b for b in response.content if b.type == "tool_use")
        migration_sql = tool_use_block.input.get("migration_sql", "")
        logger.log_environment_response(
            agent="baseline_llm",
            action="write_migration_result",
            output_data={"migration_sql": migration_sql},
        )

        guardian = DataLossGuardian()
        pre_snapshot = guardian.snapshot(sb.connection)

        migration_result = sb.apply_migration(migration_sql)
        logger.log_tool_call(
            agent="sandbox_verifier",
            tool="apply_migration",
            output_data={"success": migration_result.success, "error": migration_result.error},
        )

        data_loss_detected = False
        data_loss_details = ""
        if migration_result.success:
            post_snapshot = guardian.snapshot(sb.connection)
            # No manifest is ever passed here -- the baseline never
            # declares intentional drops, so ANY discarded data (even
            # something that would look like a legitimate deprecation)
            # is measured as loss. That's intentional: it's exactly the
            # blind spot a naive zero-shot call has, and it's the
            # number this whole comparison exists to surface.
            loss_report = guardian.compare(pre_snapshot, post_snapshot, manifest=MigrationManifest())
            data_loss_detected = not loss_report.passed
            data_loss_details = loss_report.details
            if data_loss_detected:
                logger.log_error(
                    agent="data_loss_guardian", error_type="DataLossDetected", message=data_loss_details
                )

        outcome = (
            "success"
            if migration_result.success and not data_loss_detected
            else ("data_loss_detected" if data_loss_detected else "sql_error")
        )
        logger.finalize(outcome=outcome)
        path = logger.write()

        return BaselineResult(
            case_name=case_name,
            migration_sql=migration_sql,
            sql_succeeded=migration_result.success,
            sql_error=migration_result.error,
            data_loss_detected=data_loss_detected,
            data_loss_details=data_loss_details,
            trajectory_path=path,
        )
    finally:
        sb.teardown()
