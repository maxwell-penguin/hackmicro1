"""
src/agents/synthesizer.py

The Migration Synthesizer: the first LLM-calling piece of the loop.
Takes a deterministic DriftReport (from the State Extractor) and
produces migration SQL plus a MigrationManifest declaring anything
it's intentionally discarding.

Design notes
------------
Structured output via tool use, not free-text SQL parsing: the model
returns migration_sql, intentional_drops, and allow_row_count_decrease
as typed function arguments rather than a fenced SQL block we'd have
to regex out of prose. This makes the manifest a forced, first-class
part of every response instead of something the model could forget to
mention -- which matters a lot here, since an unforced manifest is
exactly the kind of silent omission the whole project exists to catch.

The prompt encodes the core engineering rule directly (never drop-and-
recreate a column holding data) as a first line of defense. The Data
Loss Guardian is the actual enforcement mechanism if the model ignores
it anyway -- the prompt reduces how often the Constraint Resolver has
to intervene, it isn't relied on as the safety net itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import anthropic

from src.core.guardian import MigrationManifest

# Anthropic periodically retires/renames model slugs. Configurable via
# env var so a rename doesn't require a code change; confirm this still
# resolves against https://docs.claude.com if it's been a while.
DEFAULT_MODEL = os.environ.get("MIGRALOOP_MODEL", "claude-sonnet-5")

_PROPOSE_MIGRATION_TOOL = {
    "name": "propose_migration",
    "description": (
        "Propose a SQLite migration that resolves the given schema drift, "
        "plus a manifest declaring anything intentionally discarded."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reasoning": {
                "type": "string",
                "description": "Brief (2-4 sentence) explanation of the migration strategy chosen.",
            },
            "migration_sql": {
                "type": "string",
                "description": (
                    "One or more semicolon-separated SQLite DDL/DML statements that "
                    "resolve the drift. Must be directly executable: no markdown fences, "
                    "no comments, no explanatory text mixed in."
                ),
            },
            "intentional_drops": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Fully-qualified 'table.column' strings for any column whose data is "
                    "intentionally being discarded (e.g. deprecating a legacy column). "
                    "Empty array if nothing is intentionally dropped."
                ),
            },
            "allow_row_count_decrease": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Table names where the migration is expected to reduce row count on "
                    "purpose (e.g. deduplication ahead of a UNIQUE constraint). Empty array "
                    "if no table is expected to shrink."
                ),
            },
        },
        "required": ["reasoning", "migration_sql", "intentional_drops", "allow_row_count_decrease"],
    },
}

SYSTEM_PROMPT = """You are the Migration Synthesizer inside MigraLoop, an agentic \
database schema drift reconciler. You generate SQLite migrations that resolve the \
gap between a physical database schema and a target (ORM-intended) schema.

CRITICAL RULE: never resolve a rename, type change, or restructuring by simply \
dropping a column and adding a new one in its place. That destroys every existing \
value in that column. Instead, use the safe multi-step pattern:
  1. ADD the new column
  2. UPDATE/backfill it from the old column (with any needed CAST or transformation)
  3. DROP the old column (only after the backfill)

SQLite-specific constraints you must respect:
- No ALTER COLUMN. To change a type, add a new column, backfill with CAST, drop the old one.
- ALTER TABLE ... RENAME COLUMN is supported and safe to use directly (data is preserved
  automatically) -- prefer it over add+backfill+drop for a pure rename with no type change.
- Adding a UNIQUE constraint to an existing table requires CREATE UNIQUE INDEX (SQLite does
  not support ALTER TABLE ... ADD CONSTRAINT). If existing data would violate the constraint
  (duplicate rows), deduplicate first and declare the affected table in
  allow_row_count_decrease.
- A NOT NULL column added to a table with existing rows needs either a DEFAULT value or a
  backfill UPDATE before the NOT NULL constraint would be enforceable against those rows.

You will always be given the full target_schema_sql text in addition to a column-level diff --
some target requirements (like UNIQUE constraints) are table-level and won't appear as column
changes, so read the target schema text itself, not just the diff summary.

Every response must go through the propose_migration tool. Always populate intentional_drops \
and allow_row_count_decrease honestly -- an undeclared drop or row-count decrease will be \
rejected by a deterministic integrity check regardless of whether the migration is otherwise \
correct, so there is no benefit to omitting a declaration and every benefit to being accurate."""


@dataclass
class SynthesisResult:
    migration_sql: str
    manifest: MigrationManifest
    reasoning: str
    raw_input: dict


class MigrationSynthesizer:
    def __init__(self, client: Optional["anthropic.Anthropic"] = None, model: str = DEFAULT_MODEL):
        # Client is injectable so callers (and tests) can supply a mock
        # instead of hitting the network -- see tests/test_synthesizer.py.
        self.client = client or anthropic.Anthropic()
        self.model = model

    def synthesize(
        self,
        drift_report_text: str,
        prior_attempt_sql: Optional[str] = None,
        prior_error: Optional[str] = None,
        prior_error_type: Optional[str] = None,
    ) -> SynthesisResult:
        """
        Generate a migration. If prior_attempt_sql/prior_error are
        given, this is a self-correction retry: the prior failure is
        included in the prompt so the model fixes the specific problem
        rather than starting over blind and potentially repeating it.
        """
        user_content = self._build_user_message(
            drift_report_text, prior_attempt_sql, prior_error, prior_error_type
        )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=[_PROPOSE_MIGRATION_TOOL],
            tool_choice={"type": "tool", "name": "propose_migration"},
            messages=[{"role": "user", "content": user_content}],
        )

        tool_use_block = next(b for b in response.content if b.type == "tool_use")
        payload = tool_use_block.input

        manifest = MigrationManifest(
            intentional_drops=set(payload.get("intentional_drops", [])),
            allow_row_count_decrease=set(payload.get("allow_row_count_decrease", [])),
        )

        return SynthesisResult(
            migration_sql=payload["migration_sql"],
            manifest=manifest,
            reasoning=payload.get("reasoning", ""),
            raw_input=payload,
        )

    @staticmethod
    def _build_user_message(
        drift_report_text: str,
        prior_attempt_sql: Optional[str],
        prior_error: Optional[str],
        prior_error_type: Optional[str],
    ) -> str:
        parts = [drift_report_text]

        if prior_attempt_sql and prior_error:
            parts.append(
                "\n\nA PREVIOUS ATTEMPT FAILED. Fix the specific problem below -- "
                "do not just retry the same SQL.\n\n"
                f"Previous migration_sql:\n{prior_attempt_sql}\n\n"
                f"Error ({prior_error_type or 'unknown'}): {prior_error}"
            )

        return "\n".join(parts)
