"""
tests/test_guardian.py

Stress tests for the Data Loss Guardian's core promise: it must catch
every undeclared loss and pass every legitimate transform. Split into
two groups on purpose -- "should fail" and "should pass" -- because a
guardrail that's only ever tested against things it should reject is
just as broken as one that's never tested at all.
"""

import sqlite3

import pytest

from src.core.guardian import DataLossGuardian, MigrationManifest


def _fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@pytest.fixture
def guardian():
    return DataLossGuardian()


# ---------------------------------------------------------------------
# Should FAIL: undeclared data loss in various shapes
# ---------------------------------------------------------------------


def test_catches_full_data_loss_on_drop_and_recreate(guardian):
    """The exact failure mode MigraLoop exists to prevent: drop + add
    instead of add + backfill + drop."""
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);"
        "INSERT INTO users (user_name) VALUES ('Alice'), ('Bob');"
    )
    pre = guardian.snapshot(conn)

    conn.executescript(
        "ALTER TABLE users DROP COLUMN user_name;"
        "ALTER TABLE users ADD COLUMN full_name TEXT;"
    )
    post = guardian.snapshot(conn)

    report = guardian.compare(pre, post)
    assert not report.passed
    assert "Alice" in report.missing_values
    assert "Bob" in report.missing_values


def test_catches_partial_data_loss(guardian):
    """Only ONE row's value is silently wiped -- a bug that a naive
    'did the row count change' check would miss entirely, since the
    row count here doesn't change."""
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);"
        "INSERT INTO users (email) VALUES ('alice@x.com'), ('bob@x.com');"
    )
    pre = guardian.snapshot(conn)

    # Simulate a buggy migration that nulls out one row's value while
    # leaving row count and the other row untouched.
    conn.execute("UPDATE users SET email = NULL WHERE email = 'alice@x.com';")
    conn.commit()
    post = guardian.snapshot(conn)

    report = guardian.compare(pre, post)
    assert not report.passed
    assert "alice@x.com" in report.missing_values
    assert "bob@x.com" not in report.missing_values


def test_undeclared_row_count_decrease_fails(guardian):
    """Deduplication that isn't declared via allow_row_count_decrease
    must be treated as loss, even though total distinct content is the
    same shape as the intentional-dedup case below."""
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE tags (id INTEGER PRIMARY KEY, item_id INTEGER, tag_name TEXT);"
        "INSERT INTO tags (item_id, tag_name) VALUES (1, 'urgent'), (1, 'urgent');"
    )
    pre = guardian.snapshot(conn)

    conn.executescript(
        "DELETE FROM tags WHERE id = 2;"
        "CREATE UNIQUE INDEX idx_tags_unique ON tags(item_id, tag_name);"
    )
    post = guardian.snapshot(conn)

    report = guardian.compare(pre, post)  # no manifest passed
    assert not report.passed
    assert "tags" in report.unexplained_row_drops


def test_undeclared_column_drop_fails(guardian):
    """Dropping a real column's data without declaring it in the
    manifest must fail, even if the migration is otherwise correct."""
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, legacy_hash TEXT);"
        "INSERT INTO users (name, legacy_hash) VALUES ('Alice', 'xyz123');"
    )
    pre = guardian.snapshot(conn)

    conn.execute("ALTER TABLE users DROP COLUMN legacy_hash;")
    conn.commit()
    post = guardian.snapshot(conn)

    report = guardian.compare(pre, post)  # no manifest -- should fail
    assert not report.passed
    assert "xyz123" in report.missing_values


# ---------------------------------------------------------------------
# Should PASS: legitimate transforms
# ---------------------------------------------------------------------


def test_passes_legitimate_rename_with_backfill(guardian):
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);"
        "INSERT INTO users (user_name) VALUES ('Alice'), ('Bob');"
    )
    pre = guardian.snapshot(conn)

    conn.executescript(
        "ALTER TABLE users ADD COLUMN full_name TEXT;"
        "UPDATE users SET full_name = user_name;"
        "ALTER TABLE users DROP COLUMN user_name;"
    )
    post = guardian.snapshot(conn)

    report = guardian.compare(pre, post)
    assert report.passed


def test_passes_type_cast_representation_change(guardian):
    """'100' (TEXT) -> 100 (INTEGER) is the same underlying value and
    must not be flagged as loss + a new unrelated value."""
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, amount VARCHAR(255));"
        "INSERT INTO orders (amount) VALUES ('100'), ('250');"
    )
    pre = guardian.snapshot(conn)

    conn.executescript(
        "ALTER TABLE orders ADD COLUMN amount_int INTEGER;"
        "UPDATE orders SET amount_int = CAST(amount AS INTEGER);"
        "ALTER TABLE orders DROP COLUMN amount;"
        "ALTER TABLE orders RENAME COLUMN amount_int TO amount;"
    )
    post = guardian.snapshot(conn)

    report = guardian.compare(pre, post)
    assert report.passed


def test_passes_table_split_content_moves_across_tables(guardian):
    """Case 05: address fields move from `users` into a brand new
    `addresses` table. The Guardian must recognize the content is
    preserved even though it's no longer in the same table."""
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, street TEXT, city TEXT);"
        "INSERT INTO users (name, street, city) VALUES ('Alice', '123 Main St', 'NY');"
    )
    pre = guardian.snapshot(conn)

    conn.executescript(
        "CREATE TABLE addresses (id INTEGER PRIMARY KEY, user_id INTEGER, street TEXT, city TEXT);"
        "INSERT INTO addresses (user_id, street, city) SELECT id, street, city FROM users;"
        "ALTER TABLE users DROP COLUMN street;"
        "ALTER TABLE users DROP COLUMN city;"
    )
    post = guardian.snapshot(conn)

    report = guardian.compare(pre, post)
    assert report.passed


def test_declared_row_count_decrease_passes(guardian):
    """Same dedup scenario as the undeclared-fails test above, but this
    time the manifest declares it -- must pass."""
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE tags (id INTEGER PRIMARY KEY, item_id INTEGER, tag_name TEXT);"
        "INSERT INTO tags (item_id, tag_name) VALUES (1, 'urgent'), (1, 'urgent');"
    )
    pre = guardian.snapshot(conn)

    conn.executescript(
        "DELETE FROM tags WHERE id = 2;"
        "CREATE UNIQUE INDEX idx_tags_unique ON tags(item_id, tag_name);"
    )
    post = guardian.snapshot(conn)

    manifest = MigrationManifest(allow_row_count_decrease={"tags"})
    report = guardian.compare(pre, post, manifest=manifest)
    assert report.passed


def test_declared_column_drop_passes(guardian):
    conn = _fresh_conn()
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, legacy_hash TEXT);"
        "INSERT INTO users (name, legacy_hash) VALUES ('Alice', 'xyz123');"
    )
    pre = guardian.snapshot(conn)

    conn.execute("ALTER TABLE users DROP COLUMN legacy_hash;")
    conn.commit()
    post = guardian.snapshot(conn)

    manifest = MigrationManifest(intentional_drops={"users.legacy_hash"})
    report = guardian.compare(pre, post, manifest=manifest)
    assert report.passed
