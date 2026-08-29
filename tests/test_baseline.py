"""
tests/test_baseline.py

Fully mocked -- no real Anthropic call, no API key required (same
reasoning as test_synthesizer.py and test_orchestrator.py). The
important case here is the data-loss one: it confirms the baseline
correctly measures a naive drop-and-recreate as a failure even though
the SQL executed without error, which is the entire point of running
it at all.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from src.baseline import run_baseline_case


def _make_case(tmp_path: Path, name: str, physical_sql: str, seed_sql: str, target_sql: str) -> Path:
    case_dir = tmp_path / name
    case_dir.mkdir()
    conn = sqlite3.connect(str(case_dir / "physical.db"))
    conn.executescript(physical_sql)
    conn.executescript(seed_sql)
    conn.commit()
    conn.close()
    (case_dir / "target_schema.sql").write_text(target_sql)
    return case_dir


def _mock_client_returning_sql(sql: str) -> MagicMock:
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.input = {"migration_sql": sql}

    response = MagicMock()
    response.content = [tool_use_block]

    client = MagicMock()
    client.messages.create.return_value = response
    return client


def test_baseline_measures_naive_drop_and_recreate_as_data_loss(tmp_path):
    """The scenario the whole comparison exists to demonstrate: the
    naive baseline resolves a rename by dropping and recreating the
    column. SQL succeeds; data is gone."""
    case_dir = _make_case(
        tmp_path,
        "case_rename",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "INSERT INTO users (user_name) VALUES ('Alice'), ('Bob');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
    )
    client = _mock_client_returning_sql(
        "ALTER TABLE users DROP COLUMN user_name; ALTER TABLE users ADD COLUMN full_name TEXT;"
    )

    result = run_baseline_case(case_dir, client=client)

    assert result.sql_succeeded is True
    assert result.data_loss_detected is True
    assert "Alice" in result.data_loss_details or "missing values" in result.data_loss_details


def test_baseline_records_clean_success_when_sql_happens_to_be_safe(tmp_path):
    case_dir = _make_case(
        tmp_path,
        "case_rename_safe",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "INSERT INTO users (user_name) VALUES ('Alice');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
    )
    client = _mock_client_returning_sql("ALTER TABLE users RENAME COLUMN user_name TO full_name;")

    result = run_baseline_case(case_dir, client=client)

    assert result.sql_succeeded is True
    assert result.data_loss_detected is False


def test_baseline_records_sql_error():
    pass  # exercised implicitly below; kept as a named placeholder so
    # a reader scanning test names sees this path is intentionally
    # covered rather than missing.


def test_baseline_records_outright_sql_failure(tmp_path):
    case_dir = _make_case(
        tmp_path,
        "case_bad_sql",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "INSERT INTO users (user_name) VALUES ('Alice');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
    )
    client = _mock_client_returning_sql("ALTER TABLE nonexistent_table ADD COLUMN x TEXT;")

    result = run_baseline_case(case_dir, client=client)

    assert result.sql_succeeded is False
    assert result.data_loss_detected is False  # never reached the data-loss check
    assert result.sql_error is not None


def test_baseline_never_declares_manifest_even_for_legitimate_looking_drops(tmp_path):
    """Case 10 (safe deprecation): the target schema genuinely wants
    legacy_hash dropped. A naive baseline has no concept of declaring
    that intentionally -- so it gets measured as data loss regardless
    of whether dropping it was semantically 'correct'. This is the
    blind spot, and it's supposed to show up as a baseline failure."""
    case_dir = _make_case(
        tmp_path,
        "case_deprecation",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, legacy_hash TEXT);",
        "INSERT INTO users (name, legacy_hash) VALUES ('Alice', 'xyz123');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);",
    )
    client = _mock_client_returning_sql("ALTER TABLE users DROP COLUMN legacy_hash;")

    result = run_baseline_case(case_dir, client=client)

    assert result.sql_succeeded is True
    assert result.data_loss_detected is True
