"""Kontrakt workflowa issue-intake: separacja uprawnień i sekretów między jobami."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = yaml.safe_load(Path(".github/workflows/issue-intake.yml").read_text())


def _job(name):
    return WORKFLOW["jobs"][name]


def test_workflow_triggers_on_newly_opened_issues():
    assert WORKFLOW[True]["issues"]["types"] == ["opened"]


def test_validate_gate_precedes_analysis():
    assert _job("analyze")["needs"] == "validate"
    assert _job("apply")["needs"] == "analyze"


def test_validate_job_fails_on_provider_variable():
    job = _job("validate")
    assert job["env"]["ISSUE_INTAKE_PROVIDER"] == "${{ vars.ISSUE_INTAKE_PROVIDER }}"
    assert "exit 1" in job["steps"][0]["run"]


def test_analyze_job_is_read_only():
    assert _job("analyze")["permissions"]["issues"] == "read"


def test_apply_job_has_issue_write():
    assert _job("apply")["permissions"]["issues"] == "write"


def test_apply_job_carries_no_model_provider_credential():
    text = yaml.safe_dump(_job("apply"))
    assert "ANTHROPIC_API_KEY" not in text
    assert "OPENAI_API_KEY" not in text


def test_provider_steps_carry_only_their_matching_secret():
    steps = _job("analyze")["steps"]
    claude = next(
        s for s in steps if s.get("name", "").startswith("Analyze issue (claude")
    )
    codex = next(
        s for s in steps if s.get("name", "").startswith("Analyze issue (codex")
    )
    assert set(claude["env"]) == {"ANTHROPIC_API_KEY"}
    assert set(codex["env"]) == {"OPENAI_API_KEY"}
    assert "vars.ISSUE_INTAKE_PROVIDER" in claude["if"]
    assert "vars.ISSUE_INTAKE_PROVIDER" in codex["if"]


def test_apply_pins_issue_number_from_event_payload():
    assert _job("apply")["env"]["ISSUE_NUMBER"] == "${{ github.event.issue.number }}"
