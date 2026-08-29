"""
tests/test_evaluate.py

Unit tests for src/evaluate.py's pure comparison logic. No file I/O, no
mocking -- _baseline_verdict, _advanced_verdict, and _is_improved are
plain functions over dicts/strings, so plain fixtures are enough.
"""

from src.evaluate import _advanced_verdict, _baseline_verdict, _is_improved


def test_baseline_verdict_not_run_when_entry_missing():
    assert _baseline_verdict(None) == "not run"


def test_baseline_verdict_sql_error_when_sql_failed():
    entry = {"sql_succeeded": False, "data_loss_detected": False}
    assert _baseline_verdict(entry) == "SQL error"


def test_baseline_verdict_data_loss_when_flagged():
    entry = {"sql_succeeded": True, "data_loss_detected": True}
    assert _baseline_verdict(entry) == "DATA LOSS"


def test_baseline_verdict_ok_when_clean():
    entry = {"sql_succeeded": True, "data_loss_detected": False}
    assert _baseline_verdict(entry) == "ok"


def test_advanced_verdict_not_run_when_entry_missing():
    assert _advanced_verdict(None) == "not run"


def test_advanced_verdict_passes_through_outcome():
    assert _advanced_verdict({"outcome": "success"}) == "success"
    assert _advanced_verdict({"outcome": "failed_max_retries"}) == "failed_max_retries"


def test_is_improved_true_when_baseline_bad_and_advanced_succeeds():
    assert _is_improved("DATA LOSS", "success") is True
    assert _is_improved("SQL error", "success") is True


def test_is_improved_false_when_baseline_already_ok():
    assert _is_improved("ok", "success") is False


def test_is_improved_false_when_advanced_does_not_succeed():
    assert _is_improved("DATA LOSS", "failed_max_retries") is False
    assert _is_improved("DATA LOSS", "not run") is False
