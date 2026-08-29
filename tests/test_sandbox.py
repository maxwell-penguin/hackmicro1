"""
tests/test_sandbox.py

Tests for the isolated SQLite test harness. The two things most likely
to quietly break the whole project if wrong: foreign-key enforcement
not actually being on, and a failed migration not actually rolling
back (which would let a half-applied bad migration masquerade as a
clean failure).
"""

import os

import pytest

from src.core.sandbox import SandboxVerifier


def test_foreign_keys_are_enforced():
    """This is the single most important thing to catch early: SQLite
    defaults FK enforcement OFF per-connection. If this pragma isn't
    actually taking effect, cases 05/06-style FK violations would
    silently succeed instead of being caught."""
    with SandboxVerifier("fk_test") as sb:
        sb.load_physical_state(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE addresses (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "street TEXT, FOREIGN KEY(user_id) REFERENCES users(id));",
            "INSERT INTO users (name) VALUES ('Alice');",
        )
        result = sb.apply_migration(
            "INSERT INTO addresses (user_id, street) VALUES (999, 'nowhere');"
        )
        assert not result.success
        assert "FOREIGN KEY" in (result.error or "").upper() or result.error_type == "IntegrityError"


def test_valid_foreign_key_insert_succeeds():
    with SandboxVerifier("fk_valid_test") as sb:
        sb.load_physical_state(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE addresses (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "street TEXT, FOREIGN KEY(user_id) REFERENCES users(id));",
            "INSERT INTO users (name) VALUES ('Alice');",
        )
        result = sb.apply_migration(
            "INSERT INTO addresses (user_id, street) VALUES (1, '123 Main St');"
        )
        assert result.success


def test_failed_migration_rolls_back_completely():
    """A migration whose second statement fails must leave the
    database exactly as it was before -- no partially-applied schema
    change left behind for the next attempt to trip over."""
    with SandboxVerifier("rollback_test") as sb:
        sb.load_physical_state(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
            "INSERT INTO users (name) VALUES ('Alice');",
        )
        result = sb.apply_migration(
            "ALTER TABLE users ADD COLUMN email TEXT;"
            "INSERT INTO nonexistent_table (x) VALUES (1);"  # fails
        )
        assert not result.success

        # the ADD COLUMN should NOT have persisted past the rollback
        cols = [row[1] for row in sb.connection.execute("PRAGMA table_info(users);")]
        assert "email" not in cols


def test_successful_migration_persists():
    with SandboxVerifier("persist_test") as sb:
        sb.load_physical_state(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
            "INSERT INTO users (name) VALUES ('Alice');",
        )
        result = sb.apply_migration("ALTER TABLE users ADD COLUMN email TEXT;")
        assert result.success
        cols = [row[1] for row in sb.connection.execute("PRAGMA table_info(users);")]
        assert "email" in cols


def test_integration_check_catches_broken_table():
    with SandboxVerifier("integration_test") as sb:
        sb.load_physical_state(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
            "INSERT INTO users (name) VALUES ('Alice');",
        )
        ok = sb.run_integration_check("users")
        assert ok.success

        missing = sb.run_integration_check("table_that_does_not_exist")
        assert not missing.success


def test_teardown_removes_temp_file():
    sb = SandboxVerifier("cleanup_test", use_temp_file=True)
    sb.provision()
    path = sb._db_path
    assert os.path.exists(path)
    sb.teardown()
    assert not os.path.exists(path)


def test_sandboxes_are_isolated_from_each_other():
    """Two sandboxes for different cases must never share state, even
    if they run back-to-back."""
    with SandboxVerifier("case_a") as sb_a:
        sb_a.load_physical_state(
            "CREATE TABLE t (id INTEGER PRIMARY KEY);", "INSERT INTO t (id) VALUES (1);"
        )
        with SandboxVerifier("case_b") as sb_b:
            # sb_b never had `t` created -- confirms no shared file/state
            tables_b = sb_b.list_tables()
            assert "t" not in tables_b
