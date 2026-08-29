"""
tests/test_orchestrator.py

Tests the full loop end-to-end using a mocked MigrationSynthesizer --
no real API call, no API key required (same reproducibility reasoning
as test_synthesizer.py). What's actually being tested is the wiring:
does a first-try success terminate correctly, does a failing first
attempt actually get retried with the right error context, does
exhausting all attempts report the right outcome, does a no-drift case
short-circuit before ever calling the LLM.
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from src.agents.synthesizer import SynthesisResult
from src.core.guardian import MigrationManifest
from src.orchestrator import run_case


def _make_case(tmp_path: Path, name: str, physical_sql: str, seed_sql: str, target_sql: str) -> Path:
    case_dir = tmp_path / name
    case_dir.mkdir()
    conn = sqlite3.connect(str(case_dir / "physical.db"))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(physical_sql)
    conn.executescript(seed_sql)
    conn.commit()
    conn.close()
    (case_dir / "target_schema.sql").write_text(target_sql)
    return case_dir


def _mock_synthesizer(results: list) -> MagicMock:
    """A MigrationSynthesizer stand-in whose .synthesize() returns each
    entry in `results` in order, one per call."""
    mock = MagicMock()
    mock.synthesize.side_effect = results
    return mock


def _result(sql: str, intentional_drops=None, allow_row_count_decrease=None) -> SynthesisResult:
    return SynthesisResult(
        migration_sql=sql,
        manifest=MigrationManifest(
            intentional_drops=set(intentional_drops or []),
            allow_row_count_decrease=set(allow_row_count_decrease or []),
        ),
        reasoning="test",
        raw_input={},
    )


def test_success_on_first_attempt(tmp_path):
    case_dir = _make_case(
        tmp_path,
        "case_rename",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "INSERT INTO users (user_name) VALUES ('Alice'), ('Bob');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
    )
    synth = _mock_synthesizer(
        [_result("ALTER TABLE users RENAME COLUMN user_name TO full_name;")]
    )

    result = run_case(case_dir, synthesizer=synth)

    assert result.outcome == "success"
    assert result.attempts == 1
    assert synth.synthesize.call_count == 1


def test_retries_after_data_loss_and_succeeds_second_attempt(tmp_path):
    case_dir = _make_case(
        tmp_path,
        "case_rename_retry",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "INSERT INTO users (user_name) VALUES ('Alice'), ('Bob');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
    )
    # first attempt: the exact bad pattern the whole project exists to catch
    bad_attempt = _result(
        "ALTER TABLE users DROP COLUMN user_name; ALTER TABLE users ADD COLUMN full_name TEXT;"
    )
    good_attempt = _result("ALTER TABLE users RENAME COLUMN user_name TO full_name;")
    synth = _mock_synthesizer([bad_attempt, good_attempt])

    result = run_case(case_dir, synthesizer=synth)

    assert result.outcome == "success"
    assert result.attempts == 2
    assert synth.synthesize.call_count == 2
    assert len(result.attempt_errors) == 1
    assert "DataLossDetected" in result.attempt_errors[0]

    # confirm the retry actually received the prior error, not a blind retry
    second_call_kwargs = synth.synthesize.call_args_list[1].kwargs
    assert second_call_kwargs["prior_error_type"] == "DataLossDetected"
    assert second_call_kwargs["prior_attempt_sql"] == bad_attempt.migration_sql


def test_exhausts_retries_and_reports_failed_max_retries(tmp_path):
    case_dir = _make_case(
        tmp_path,
        "case_always_bad",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "INSERT INTO users (user_name) VALUES ('Alice');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
    )
    always_bad = _result(
        "ALTER TABLE users DROP COLUMN user_name; ALTER TABLE users ADD COLUMN full_name TEXT;"
    )
    synth = _mock_synthesizer([always_bad, always_bad, always_bad])

    result = run_case(case_dir, synthesizer=synth)

    assert result.outcome == "failed_max_retries"
    assert result.attempts == 3
    assert synth.synthesize.call_count == 3
    assert len(result.attempt_errors) == 3


def test_no_drift_short_circuits_without_calling_synthesizer(tmp_path):
    schema = "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
    case_dir = _make_case(tmp_path, "case_no_drift", schema, "", schema)
    synth = _mock_synthesizer([])

    result = run_case(case_dir, synthesizer=synth)

    assert result.outcome == "no_drift"
    assert result.attempts == 0
    assert synth.synthesize.call_count == 0


def test_trajectory_is_written_and_reports_success_outcome(tmp_path):
    import json
    import os

    case_dir = _make_case(
        tmp_path,
        "case_trajectory_check",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, user_name TEXT);",
        "INSERT INTO users (user_name) VALUES ('Alice');",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT);",
    )
    synth = _mock_synthesizer(
        [_result("ALTER TABLE users RENAME COLUMN user_name TO full_name;")]
    )

    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = run_case(case_dir, synthesizer=synth)
        assert result.trajectory_path is not None
        with open(result.trajectory_path) as f:
            record = json.load(f)
        assert record["outcome"] == "success"
        assert len(record["steps"]) > 0
    finally:
        os.chdir(original_cwd)
