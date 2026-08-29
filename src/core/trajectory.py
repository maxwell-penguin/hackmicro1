"""
src/core/trajectory.py

Structured trace logger for MigraLoop's agent loop. Produces the
mandatory "Agent Trajectories" deliverable: a JSON record per case
showing every agent step, tool call, environment response, and
self-correction — not just the final diff. Log this from inside every
agent as it runs, not after the fact; reconstructing a trajectory after
the loop finishes loses exactly the failure-and-recovery detail the
rubric is asking to see.

Usage
-----
    logger = TrajectoryLogger(case_name="02_rename_column_with_data")

    logger.log_step(
        agent="synthesizer",
        action="generate_migration",
        input_data={"drift_report": drift_report},
        output_data={"migration_sql": migration_sql, "manifest": manifest},
    )
    logger.log_tool_call(
        agent="sandbox_verifier",
        tool="apply_migration",
        input_data={"migration_sql": migration_sql},
        output_data={"success": result.success, "error": result.error},
    )
    logger.log_error(
        agent="sandbox_verifier",
        error_type="IntegrityError",
        message=result.error,
    )
    logger.log_self_correction(
        agent="constraint_resolver",
        prior_error=result.error,
        correction_strategy="add explicit USING clause for type cast",
        new_input={"migration_sql": corrected_sql},
    )
    logger.finalize(outcome="success")
    logger.write()  # -> trajectories/02_rename_column_with_data.json
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# Not enforced via typing.Literal at runtime (kept dependency-free /
# plain dataclasses throughout core/), but these are the only kinds a
# step should be logged as:
#   "reasoning"             - an agent's intermediate decision/plan
#   "tool_call"              - an agent invoking sandbox/guardian/etc.
#   "environment_response"   - a result coming back from the sandbox
#   "error"                  - a failure surfaced to the loop
#   "self_correction"        - the resolver's retry with corrected input
_VALID_KINDS = {"reasoning", "tool_call", "environment_response", "error", "self_correction"}


@dataclass
class TrajectoryStep:
    step_id: str
    kind: str
    agent: str
    action: str
    timestamp: float
    input_data: dict = field(default_factory=dict)
    output_data: dict = field(default_factory=dict)
    error_type: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class TrajectoryRecord:
    case_name: str
    run_id: str
    started_at: float
    ended_at: Optional[float] = None
    outcome: Optional[str] = None  # "success" | "failed" | "data_loss_detected"
    attempt_count: int = 0
    steps: list = field(default_factory=list)


class TrajectoryLogger:
    """One logger instance per benchmark-case run (baseline or advanced)."""

    def __init__(self, case_name: str, output_dir: str = "trajectories"):
        self.output_dir = output_dir
        self._record = TrajectoryRecord(
            case_name=case_name,
            run_id=uuid.uuid4().hex[:12],
            started_at=time.time(),
        )

    # -- logging -------------------------------------------------------

    def log_step(
        self,
        agent: str,
        action: str,
        input_data: Optional[dict] = None,
        output_data: Optional[dict] = None,
        kind: str = "reasoning",
    ) -> None:
        assert kind in _VALID_KINDS, f"invalid trajectory step kind: {kind}"
        self._record.steps.append(
            TrajectoryStep(
                step_id=uuid.uuid4().hex[:8],
                kind=kind,
                agent=agent,
                action=action,
                timestamp=time.time(),
                input_data=input_data or {},
                output_data=output_data or {},
            )
        )

    def log_tool_call(
        self,
        agent: str,
        tool: str,
        input_data: Optional[dict] = None,
        output_data: Optional[dict] = None,
    ) -> None:
        self.log_step(agent=agent, action=tool, input_data=input_data, output_data=output_data, kind="tool_call")

    def log_environment_response(self, agent: str, action: str, output_data: dict) -> None:
        self.log_step(agent=agent, action=action, output_data=output_data, kind="environment_response")

    def log_error(self, agent: str, error_type: str, message: str, action: str = "error") -> None:
        self._record.steps.append(
            TrajectoryStep(
                step_id=uuid.uuid4().hex[:8],
                kind="error",
                agent=agent,
                action=action,
                timestamp=time.time(),
                error_type=error_type,
                error_message=message,
            )
        )

    def log_self_correction(
        self, agent: str, prior_error: str, correction_strategy: str, new_input: dict
    ) -> None:
        self._record.attempt_count += 1
        self.log_step(
            agent=agent,
            action="self_correction",
            input_data={"prior_error": prior_error, "strategy": correction_strategy, **new_input},
            kind="self_correction",
        )

    # -- finalize / persist -------------------------------------------

    def finalize(self, outcome: str) -> None:
        self._record.outcome = outcome
        self._record.ended_at = time.time()

    def write(self) -> str:
        """Write the trajectory to disk as JSON, return the file path."""
        os.makedirs(self.output_dir, exist_ok=True)
        path = os.path.join(self.output_dir, f"{self._record.case_name}.json")
        with open(path, "w") as f:
            json.dump(asdict(self._record), f, indent=2, default=str)
        return path

    @property
    def record(self) -> TrajectoryRecord:
        return self._record
