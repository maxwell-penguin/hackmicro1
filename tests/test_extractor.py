"""
tests/test_extractor.py

Tests the State Extractor against the actual 6 benchmark scenarios --
not synthetic examples, the exact physical/target pairs the Synthesizer
will receive during the real evaluation run. If the diff is wrong here,
every downstream agent is reasoning from bad information.
"""

import sqlite3

from src.agents.extractor import StateExtractor


def _physical_conn(schema_sql: str, seed_sql: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(schema_sql)
    if seed_sql:
        conn.executescript(seed_sql)
    return conn


def test_case_01_simple_add_detects_new_column():
    conn = _physical_conn("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
    target = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);"

    report = StateExtractor().extract_drift(conn, target)
    assert report.has_drift

    users_diff = next(t for t in report.tables if t.table_name == "users")
    assert users_diff.status == "modified"
    assert [c.name for c in users_diff.columns_added] == ["email"]
    assert not users_diff.columns_removed


def test_case_02_rename_detects_removed_and_added_column():
    conn = _physical_conn("CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);")
    target = "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);"

    report = StateExtractor().extract_drift(conn, target)
    users_diff = next(t for t in report.tables if t.table_name == "users")

    assert users_diff.status == "modified"
    assert [c.name for c in users_diff.columns_removed] == ["user_name"]
    assert [c.name for c in users_diff.columns_added] == ["full_name"]
    # The extractor deliberately does NOT try to guess this is a
    # "rename" -- that's a semantic judgment call for the Synthesizer,
    # not a structural fact. A remove + an add is exactly what happened.


def test_case_03_type_migration_detects_type_change():
    conn = _physical_conn(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount VARCHAR(255));"
    )
    target = "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER);"

    report = StateExtractor().extract_drift(conn, target)
    orders_diff = next(t for t in report.tables if t.table_name == "orders")

    assert orders_diff.status == "modified"
    assert len(orders_diff.type_changes) == 1
    tc = orders_diff.type_changes[0]
    assert tc.column == "amount"
    assert tc.old_type.upper() == "VARCHAR(255)"
    assert tc.new_type.upper() == "INTEGER"


def test_case_05_table_split_detects_added_table_and_removed_columns():
    conn = _physical_conn(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, street TEXT, city TEXT);"
    )
    target = (
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE addresses (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "street TEXT, city TEXT, FOREIGN KEY(user_id) REFERENCES users(id));"
    )

    report = StateExtractor().extract_drift(conn, target)

    users_diff = next(t for t in report.tables if t.table_name == "users")
    assert users_diff.status == "modified"
    assert {c.name for c in users_diff.columns_removed} == {"street", "city"}

    addresses_diff = next(t for t in report.tables if t.table_name == "addresses")
    assert addresses_diff.status == "added"
    assert addresses_diff.create_sql is not None
    assert "addresses" in addresses_diff.create_sql.lower()


def test_case_08_composite_unique_index_detects_table_level_drift():
    """Adding a UNIQUE constraint via a new index (rather than an
    inline column property) has an identical column list on both
    sides -- a pure column-level diff would miss it entirely, which
    was a real bug: the orchestrator's has_drift gate never attempted
    a migration for this case because no column-level change was
    detected. Comparing the raw CREATE TABLE text catches it."""
    conn = _physical_conn(
        "CREATE TABLE tags (id INTEGER PRIMARY KEY, item_id INTEGER, tag_name TEXT);",
        "INSERT INTO tags (item_id, tag_name) VALUES (1, 'urgent'), (1, 'urgent');",
    )
    target = (
        "CREATE TABLE tags (id INTEGER PRIMARY KEY, item_id INTEGER, tag_name TEXT, "
        "UNIQUE(item_id, tag_name));"
    )

    report = StateExtractor().extract_drift(conn, target)
    assert report.has_drift

    tags_diff = next(t for t in report.tables if t.table_name == "tags")
    assert tags_diff.status == "modified"
    assert tags_diff.table_definition_changed is True
    # no column added/removed/type-changed -- the UNIQUE constraint is
    # table-level metadata, not a column property
    assert not tags_diff.columns_added
    assert not tags_diff.columns_removed
    assert not tags_diff.type_changes

    text = report.to_prompt_text()
    assert "table-level definition differs" in text


def test_case_10_safe_deprecation_detects_column_removal():
    conn = _physical_conn(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, legacy_hash TEXT);"
    )
    target = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"

    report = StateExtractor().extract_drift(conn, target)
    users_diff = next(t for t in report.tables if t.table_name == "users")

    assert users_diff.status == "modified"
    assert [c.name for c in users_diff.columns_removed] == ["legacy_hash"]
    assert not users_diff.columns_added


def test_no_drift_reports_unchanged():
    schema = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
    conn = _physical_conn(schema)

    report = StateExtractor().extract_drift(conn, schema)
    assert not report.has_drift
    assert all(t.status == "unchanged" for t in report.tables)


def test_prompt_text_omits_unchanged_tables():
    conn = _physical_conn(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE untouched (id INTEGER PRIMARY KEY);"
    )
    target = (
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT);"
        "CREATE TABLE untouched (id INTEGER PRIMARY KEY);"
    )

    report = StateExtractor().extract_drift(conn, target)
    text = report.to_prompt_text()

    assert "users" in text
    assert "untouched" not in text
