"""Intake triage: czysta logika wyboru labelki, duplikatu i planu apply."""

from __future__ import annotations

import pytest
from scripts import issue_intake as intake

REPO_LABELS = [
    {"name": "bug", "description": "Something isn't working"},
    {"name": "pipeline", "description": "ASR, diarization, overlap recovery"},
    {"name": "webapp", "description": "FastAPI app, Jinja2 templates, auth, job queue"},
    {"name": "process", "description": "Repo development process"},
    {"name": "duplicate", "description": "This issue already exists"},
]

CANDIDATES = ["pipeline", "webapp", "process"]


def test_area_candidates_intersect_taxonomy_with_live_labels():
    names = [c["name"] for c in intake.area_candidates(REPO_LABELS)]
    assert names == ["pipeline", "webapp", "process"]


def test_area_candidates_empty_when_taxonomy_absent():
    assert intake.area_candidates([{"name": "bug", "description": ""}]) == []


def test_parse_model_json_plain():
    assert intake.parse_model_json('{"area_label": "webapp"}') == {
        "area_label": "webapp"
    }


def test_parse_model_json_fenced():
    raw = '```json\n{"area_label": "docs"}\n```'
    assert intake.parse_model_json(raw) == {"area_label": "docs"}


def test_parse_model_json_with_prose_around():
    raw = 'Sure! Here is the JSON:\n{"area_label": "infra"}\nHope that helps.'
    assert intake.parse_model_json(raw) == {"area_label": "infra"}


@pytest.mark.parametrize("raw", ["", "no json here", '{"area_label": "webapp"', "[]"])
def test_parse_model_json_garbage_returns_none(raw):
    assert intake.parse_model_json(raw) is None


def test_normalize_result_happy_path_with_duplicate():
    out = intake.normalize_result(
        {
            "area_label": "webapp",
            "duplicate": {"number": 3, "confidence": "high", "reason": "same bug"},
        },
        CANDIDATES,
        {3, 7},
    )
    assert out["area_label"] == "webapp"
    assert out["duplicate"] == {"number": 3, "confidence": "high", "reason": "same bug"}


def test_normalize_result_strips_reason_whitespace_and_caps_confidence():
    out = intake.normalize_result(
        {
            "area_label": "webapp",
            "duplicate": {"number": 3, "confidence": "HIGH", "reason": "  x  "},
        },
        CANDIDATES,
        {3},
    )
    assert out["duplicate"]["confidence"] == "high"
    assert out["duplicate"]["reason"] == "x"


def test_normalize_result_unknown_label_raises():
    with pytest.raises(ValueError):
        intake.normalize_result({"area_label": "salesforce"}, CANDIDATES, {3})


def test_normalize_result_none_input_raises():
    with pytest.raises(ValueError):
        intake.normalize_result(None, CANDIDATES, {3})


def test_normalize_result_drops_duplicate_outside_open_issues():
    out = intake.normalize_result(
        {
            "area_label": "webapp",
            "duplicate": {"number": 99, "confidence": "high", "reason": "x"},
        },
        CANDIDATES,
        {3},
    )
    assert out["duplicate"] is None


def test_normalize_result_defaults_confidence_to_low():
    out = intake.normalize_result(
        {"area_label": "webapp", "duplicate": {"number": 3, "reason": "x"}},
        CANDIDATES,
        {3},
    )
    assert out["duplicate"]["confidence"] == "low"


def test_normalize_result_none_duplicate_ok():
    out = intake.normalize_result(
        {"area_label": "process", "duplicate": None}, CANDIDATES, set()
    )
    assert out == {"area_label": "process", "duplicate": None}


RESULT = {
    "issue_number": 5,
    "area_label": "webapp",
    "duplicate": {"number": 3, "confidence": "high", "reason": "same report"},
}


def test_plan_apply_adds_area_and_duplicate_labels_when_closing():
    plan = intake.plan_apply(
        RESULT,
        trigger_number=5,
        live_labels={"webapp", "duplicate"},
        duplicate_target={"html_url": "u", "state": "open"},
    )
    assert plan["add_labels"] == ["webapp", "duplicate"]


def test_plan_apply_mismatched_issue_number_raises():
    with pytest.raises(ValueError):
        intake.plan_apply(
            RESULT, trigger_number=6, live_labels={"webapp"}, duplicate_target=None
        )


def test_plan_apply_label_not_live_raises():
    with pytest.raises(ValueError):
        intake.plan_apply(
            RESULT, trigger_number=5, live_labels={"bug"}, duplicate_target=None
        )


def test_plan_apply_closes_on_high_confidence_duplicate():
    plan = intake.plan_apply(
        RESULT,
        trigger_number=5,
        live_labels={"webapp", "duplicate"},
        duplicate_target={"html_url": "https://github.com/r/i/3", "state": "open"},
    )
    assert plan["close"] is True
    assert "https://github.com/r/i/3" in plan["comment"]
    assert "#3" in plan["comment"]


def test_plan_apply_low_confidence_comments_but_does_not_close():
    result = dict(RESULT, duplicate={"number": 3, "confidence": "low", "reason": "x"})
    plan = intake.plan_apply(
        result,
        trigger_number=5,
        live_labels={"webapp"},
        duplicate_target={"html_url": "https://github.com/r/i/3", "state": "open"},
    )
    assert plan["close"] is False
    assert plan["add_labels"] == ["webapp"]
    assert plan["comment"]


def test_plan_apply_no_duplicate_no_comment_no_close():
    result = dict(RESULT, duplicate=None)
    plan = intake.plan_apply(
        result, trigger_number=5, live_labels={"webapp"}, duplicate_target=None
    )
    assert plan == {"add_labels": ["webapp"], "comment": None, "close": False}


def test_plan_apply_drops_duplicate_when_target_not_open():
    plan = intake.plan_apply(
        RESULT, trigger_number=5, live_labels={"webapp"}, duplicate_target=None
    )
    assert plan == {"add_labels": ["webapp"], "comment": None, "close": False}


def test_plan_apply_skips_duplicate_label_if_repo_lacks_it():
    plan = intake.plan_apply(
        RESULT,
        trigger_number=5,
        live_labels={"webapp"},
        duplicate_target={"html_url": "u", "state": "open"},
    )
    assert plan["add_labels"] == ["webapp"]
    assert plan["close"] is True


def test_truncate_keeps_short_text():
    assert intake.truncate("abc", 10) == "abc"


def test_truncate_cuts_long_text_with_marker():
    out = intake.truncate("a" * 50, 10)
    assert len(out) <= 10
    assert out.endswith("…")


def test_build_prompt_includes_issue_and_candidates():
    issue = {"number": 5, "title": "Fix sync", "body": "Sync fails at midnight"}
    open_issues = [{"number": 3, "title": "Sync broken", "body": "fails"}]
    prompt = intake.build_prompt(
        issue, [{"name": "webapp", "description": "FastAPI app"}], open_issues
    )
    assert "Fix sync" in prompt
    assert "webapp" in prompt
    assert "#3" in prompt
    assert "JSON" in prompt
