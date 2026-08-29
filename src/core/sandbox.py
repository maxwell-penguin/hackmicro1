"""
src/core/sandbox.py

Deterministic, isolated SQLite test harness for MigraLoop.

Each SandboxVerifier instance owns exactly one throwaway SQLite database
(a temp file, by default) that is seeded with a case's physical schema
and seed data, then used to apply and verify a candidate migration in
isolation from any other case or run.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Optional


class SandboxError(Exception):
    """Raised for sandbox provisioning failures (not migration failures —
    those are returned as a MigrationResult, not raised)."""


@dataclass
class MigrationResult:
    success: bool
    error: Optional[str] = None
    error_type: Optional[str] = None  # e.g. "IntegrityError", "OperationalError"
    statements_executed: int = 0
    executed_sql: list = field(default_factory=list)


class SandboxVerifier:
    """
    Provisions an isolated SQLite database, applies a case's physical
    schema + seed data, and exposes methods to run a synthesized
    migration and inspect the resulting state.

    Uses a temp *file* by default (not :memory:) so a run's on-disk
    state can be inspected for debugging if something fails mid-build,
    and so nothing is shared across sandbox instances even if they're
    used concurrently.
    """

    def __init__(self, case_name: str, use_temp_file: bool = True):
        self.case_name = case_name
        self._use_temp_file = use_temp_file
        self._db_path: Optional[str] = None
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "SandboxVerifier":
        self.provision()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.teardown()

    def provision(self, source_db_path: Optional[str] = None) -> None:
        """
        Create a fresh database file/connection for this sandbox. If
        source_db_path is given, the sandbox's temp file starts as a
        byte-for-byte copy of that file (used to load a pre-built
        benchmark case's physical.db) instead of an empty database —
        the source file itself is never opened or mutated directly.
        """
        if self._use_temp_file:
            fd, path = tempfile.mkstemp(
                prefix=f"migraloop_{self.case_name}_{uuid.uuid4().hex[:8]}_",
                suffix=".db",
            )
            os.close(fd)
            if source_db_path:
                shutil.copy2(source_db_path, path)
            self._db_path = path
        else:
            if source_db_path:
                raise SandboxError(
                    "source_db_path requires use_temp_file=True "
                    "(an in-memory database can't be seeded from a file copy)"
                )
            self._db_path = ":memory:"

        self._conn = sqlite3.connect(self._db_path)
        # isolation_level=None puts the connection in autocommit mode,
        # which hands transaction control to us explicitly (BEGIN /
        # COMMIT / ROLLBACK below). This matters specifically for DDL:
        # Python's sqlite3 module otherwise auto-commits ALTER/CREATE/
        # DROP statements before a later failure is even detected, so
        # apply_migration()'s rollback on failure would silently NOT
        # undo any schema change already executed in that same call —
        # a partially-applied "failed" migration masquerading as a
        # clean failure. SQLite itself supports transactional DDL; the
        # driver's default just doesn't use it unless asked to.
        self._conn.isolation_level = None
        # SQLite defaults foreign-key enforcement OFF per-connection.
        # This must be set on every connection this sandbox opens, or
        # the FK-dependency benchmark cases will silently pass instead
        # of exercising the constraint they're meant to test.
        self._conn.execute("PRAGMA foreign_keys = ON;")

    def teardown(self) -> None:
        """Close the connection and remove the temp file, if any."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._db_path and self._db_path != ":memory:" and os.path.exists(self._db_path):
            os.remove(self._db_path)
        self._db_path = None

    # -- setup -------------------------------------------------------

    def load_physical_state(self, schema_sql: str, seed_sql: str) -> None:
        """Apply the case's starting (physical/drifted) schema + seed data."""
        self._require_conn()
        try:
            self._conn.executescript(schema_sql)
            self._conn.executescript(seed_sql)
        except sqlite3.Error as e:
            raise SandboxError(
                f"[{self.case_name}] failed to load physical state: {e}"
            ) from e

    # -- migration execution -------------------------------------------

    def apply_migration(self, migration_sql: str) -> MigrationResult:
        """
        Execute a candidate migration's SQL statements inside an
        explicit transaction (see the isolation_level note in
        provision()) so DDL failures actually roll back. Returns a
        MigrationResult rather than raising, so callers (agents,
        evaluator) can inspect failures programmatically instead of
        catching exceptions everywhere.
        """
        self._require_conn()
        statements = self._split_statements(migration_sql)
        executed = []

        self._conn.execute("BEGIN;")
        try:
            for stmt in statements:
                self._conn.execute(stmt)
                executed.append(stmt)
            self._conn.execute("COMMIT;")
            return MigrationResult(
                success=True,
                statements_executed=len(executed),
                executed_sql=executed,
            )
        except sqlite3.Error as e:
            self._conn.execute("ROLLBACK;")
            return MigrationResult(
                success=False,
                error=str(e),
                error_type=type(e).__name__,
                statements_executed=len(executed),
                executed_sql=executed,
            )

    def run_integration_check(self, table_name: str) -> MigrationResult:
        """
        Minimal post-migration smoke test: confirm the table is
        queryable at all. Catches migrations that "succeeded"
        syntactically but left an unusable table (e.g. wrong final
        column set after a multi-step backfill).
        """
        self._require_conn()
        try:
            self._conn.execute(f"SELECT * FROM {table_name} LIMIT 1;")
            return MigrationResult(success=True)
        except sqlite3.Error as e:
            return MigrationResult(success=False, error=str(e), error_type=type(e).__name__)

    # -- introspection -------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        self._require_conn()
        return self._conn

    def list_tables(self) -> list:
        self._require_conn()
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
        return [r[0] for r in rows]

    # -- helpers -----------------------------------------------------

    def _require_conn(self) -> None:
        if self._conn is None:
            raise SandboxError(
                f"[{self.case_name}] sandbox not provisioned; call provision() first"
            )

    @staticmethod
    def _split_statements(sql: str) -> list:
        """
        Naive split on ';'. Good enough for the plain DDL/DML MigraLoop
        generates for this benchmark domain (no stored procedures, no
        semicolons embedded in string literals). Flagged explicitly here
        as a known limitation rather than a silent one — if the
        Synthesizer ever needs to emit a literal containing ';', this is
        the first place to fix.
        """
        return [s.strip() for s in sql.split(";") if s.strip()]
