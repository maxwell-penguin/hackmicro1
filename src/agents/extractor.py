"""
src/agents/extractor.py

The State Extractor: produces a structured DriftReport by comparing a
physical (drifted) database schema against a target (ORM-intended)
schema.

Design note
-----------
This is deliberately NOT an LLM call. Diffing two schemas is a
structural comparison with one correct answer -- asking a model to
"figure out what changed" introduces failure modes (missed columns,
hallucinated changes) for zero benefit over just introspecting both
schemas programmatically. Keeping this deterministic, like the
Guardian, means the Synthesizer's prompt is built on a diff we're
certain is correct, rather than one more probabilistic layer the
Constraint Resolver might have to debug later.

To read the *target* schema's structure without hand-writing a SQL
parser, we apply target_schema_sql to a disposable in-memory SQLite
connection and introspect the result the same way we introspect the
physical database. SQLite's own parser does the parsing; we just read
what it built.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ColumnInfo:
    name: str
    type: str
    not_null: bool
    is_pk: bool
    default: Optional[str] = None


@dataclass
class TypeChange:
    column: str
    old_type: str
    new_type: str


@dataclass
class NullabilityChange:
    column: str
    old_not_null: bool
    new_not_null: bool


@dataclass
class TableDiff:
    table_name: str
    status: str  # "added" | "removed" | "modified" | "unchanged"
    columns_added: list = field(default_factory=list)      # list[ColumnInfo]
    columns_removed: list = field(default_factory=list)    # list[ColumnInfo]
    type_changes: list = field(default_factory=list)       # list[TypeChange]
    nullability_changes: list = field(default_factory=list)  # list[NullabilityChange]
    create_sql: Optional[str] = None  # populated for "added" tables


@dataclass
class DriftReport:
    tables: list  # list[TableDiff]
    physical_schema_sql: str  # exact CREATE statements as SQLite stored them
    target_schema_sql: str    # as provided

    @property
    def has_drift(self) -> bool:
        return any(t.status != "unchanged" for t in self.tables)

    def to_prompt_text(self) -> str:
        """
        Render the diff as plain text suitable for handing to the
        Migration Synthesizer's prompt. Keeps the LLM's input to
        exactly the facts of the diff -- no editorializing about how
        to fix it, that's the Synthesizer's job.
        """
        lines = ["SCHEMA DRIFT REPORT", "=" * 20, ""]
        for t in self.tables:
            if t.status == "unchanged":
                continue
            lines.append(f"Table: {t.table_name} [{t.status}]")
            if t.status == "added":
                lines.append(f"  New table. Target definition:\n    {t.create_sql}")
            elif t.status == "removed":
                lines.append("  Table exists physically but is absent from the target schema.")
            else:
                for c in t.columns_added:
                    nn = " NOT NULL" if c.not_null else ""
                    lines.append(f"  + column added: {c.name} {c.type}{nn}")
                for c in t.columns_removed:
                    lines.append(f"  - column removed: {c.name} {c.type}")
                for tc in t.type_changes:
                    lines.append(f"  ~ type changed: {tc.column} {tc.old_type} -> {tc.new_type}")
                for nc in t.nullability_changes:
                    lines.append(
                        f"  ~ nullability changed: {nc.column} "
                        f"NOT NULL={nc.old_not_null} -> NOT NULL={nc.new_not_null}"
                    )
            lines.append("")
        if not any(t.status != "unchanged" for t in self.tables):
            lines.append("(no drift detected)")
        return "\n".join(lines)


class StateExtractor:
    """Deterministic schema introspection and diffing."""

    def extract_drift(self, physical_conn: sqlite3.Connection, target_schema_sql: str) -> DriftReport:
        physical_tables = self._introspect(physical_conn)
        physical_create_sql = self._dump_create_statements(physical_conn)

        target_conn = sqlite3.connect(":memory:")
        try:
            target_conn.executescript(target_schema_sql)
            target_tables = self._introspect(target_conn)
            target_create_by_name = {
                name: sql for name, sql in self._table_create_statements(target_conn)
            }
        finally:
            target_conn.close()

        all_table_names = set(physical_tables) | set(target_tables)
        diffs = []

        for table_name in sorted(all_table_names):
            phys_cols = physical_tables.get(table_name)
            targ_cols = target_tables.get(table_name)

            if phys_cols is None:
                diffs.append(
                    TableDiff(
                        table_name=table_name,
                        status="added",
                        create_sql=target_create_by_name.get(table_name),
                    )
                )
                continue

            if targ_cols is None:
                diffs.append(TableDiff(table_name=table_name, status="removed"))
                continue

            diff = self._diff_columns(table_name, phys_cols, targ_cols)
            diffs.append(diff)

        return DriftReport(
            tables=diffs,
            physical_schema_sql=physical_create_sql,
            target_schema_sql=target_schema_sql,
        )

    # -- diffing -------------------------------------------------------

    @staticmethod
    def _diff_columns(table_name: str, phys_cols: dict, targ_cols: dict) -> TableDiff:
        phys_names = set(phys_cols)
        targ_names = set(targ_cols)

        columns_added = [targ_cols[n] for n in sorted(targ_names - phys_names)]
        columns_removed = [phys_cols[n] for n in sorted(phys_names - targ_names)]

        type_changes = []
        nullability_changes = []
        for name in sorted(phys_names & targ_names):
            p, t = phys_cols[name], targ_cols[name]
            if p.type.strip().upper() != t.type.strip().upper():
                type_changes.append(TypeChange(column=name, old_type=p.type, new_type=t.type))
            if p.not_null != t.not_null:
                nullability_changes.append(
                    NullabilityChange(column=name, old_not_null=p.not_null, new_not_null=t.not_null)
                )

        has_changes = bool(columns_added or columns_removed or type_changes or nullability_changes)
        return TableDiff(
            table_name=table_name,
            status="modified" if has_changes else "unchanged",
            columns_added=columns_added,
            columns_removed=columns_removed,
            type_changes=type_changes,
            nullability_changes=nullability_changes,
        )

    # -- introspection -------------------------------------------------

    @staticmethod
    def _introspect(conn: sqlite3.Connection) -> dict:
        """table_name -> {column_name: ColumnInfo}"""
        result = {}
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
        for (table_name,) in rows:
            columns = {}
            for col in conn.execute(f"PRAGMA table_info({table_name});").fetchall():
                # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
                _, name, col_type, notnull, default, pk = col
                columns[name] = ColumnInfo(
                    name=name,
                    type=col_type or "",
                    not_null=bool(notnull),
                    is_pk=bool(pk),
                    default=default,
                )
            result[table_name] = columns
        return result

    @staticmethod
    def _table_create_statements(conn: sqlite3.Connection):
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
        return rows

    @staticmethod
    def _dump_create_statements(conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL;"
        ).fetchall()
        return "\n".join(r[0] + ";" for r in rows)
