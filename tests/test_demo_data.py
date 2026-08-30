"""
tests/test_demo_data.py

Unit tests for demo/generate_demo_data.py's pure transform logic
(build_comparison, extract_hot_take). No file I/O beyond what
build_demo_data itself needs -- covered separately by exercising it
against the repo's real committed results/trajectories.
"""

from demo.generate_demo_data import build_comparison, build_demo_data, extract_hot_take


def test_build_comparison_flags_improvement_on_data_loss_to_success():
    baseline = {"08_x": {"sql_succeeded": True, "data_loss_detected": True}}
    advanced = {"08_x": {"outcome": "success"}}
    rows = build_comparison(baseline, advanced)
    assert rows == [{"case": "08_x", "baseline": "DATA LOSS", "advanced": "success", "improved": True}]


def test_build_comparison_no_improvement_when_baseline_already_ok():
    baseline = {"01_x": {"sql_succeeded": True, "data_loss_detected": False}}
    advanced = {"01_x": {"outcome": "success"}}
    rows = build_comparison(baseline, advanced)
    assert rows[0]["improved"] is False


def test_build_comparison_sorts_by_case_name_and_unions_both_sides():
    baseline = {"b_only": {"sql_succeeded": True, "data_loss_detected": False}}
    advanced = {"a_only": {"outcome": "success"}}
    rows = build_comparison(baseline, advanced)
    assert [r["case"] for r in rows] == ["a_only", "b_only"]
    assert rows[0]["baseline"] == "not run"
    assert rows[1]["advanced"] == "not run"


def test_extract_hot_take_pulls_section_up_to_next_heading():
    readme = "# Title\n\n## Hot Take\n\nLine one.\nLine two.\n\n## Reproduction\n\nignored\n"
    assert extract_hot_take(readme) == "Line one.\nLine two."


def test_extract_hot_take_missing_section_raises():
    try:
        extract_hot_take("# Title\n\nno hot take here\n")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_demo_data_against_real_repo_files():
    """Grounding check: the generator must run cleanly against this
    repo's actual committed results/trajectories/README, not just
    synthetic fixtures."""
    data = build_demo_data()
    assert data["replay_case"] == "08_composite_unique_index"
    assert data["replay_baseline_trajectory"]["outcome"] == "data_loss_detected"
    assert data["replay_advanced_trajectory"]["outcome"] == "success"
    assert len(data["comparison"]) == 6
    assert "retry loop" in data["hot_take"]
