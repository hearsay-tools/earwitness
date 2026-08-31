"""Kontrakt workflowa automated-code-review: provider, uprawnienia, cap 3 rund."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = Path(".github/workflows/automated-code-review.yml")
PROMPT_PATH = Path(".github/codex/prompts/review.md")
SCHEMA_PATH = Path(".github/schemas/review-findings.schema.json")
PUBLISHER_PATH = Path(".github/scripts/automated-review.js")
WORKFLOWS_DIR = Path(".github/workflows")


@pytest.fixture(scope="module")
def workflow():
    if not WORKFLOW_PATH.is_file():
        pytest.fail(f"missing {WORKFLOW_PATH}")
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def _job(workflow, name):
    return workflow["jobs"][name]


def _dump(job):
    return yaml.safe_dump(job)


def test_workflow_triggers_on_pull_request_target_including_drafts(workflow):
    trigger = workflow[True]["pull_request_target"]
    assert trigger["types"] == ["opened", "synchronize", "reopened"]
    assert trigger["branches"] == ["main"]


def test_validate_gate_precedes_provider_jobs(workflow):
    assert "validate-provider" in _job(workflow, "claude-review")["needs"]
    assert "validate-provider" in _job(workflow, "codex-review")["needs"]
    assert "review-round" in _job(workflow, "claude-review")["needs"]
    assert "review-round" in _job(workflow, "codex-review")["needs"]


def test_validate_job_fails_on_provider_variable(workflow):
    text = _dump(_job(workflow, "validate-provider"))
    assert "${{ vars.AUTOMATED_REVIEWER }}" in text
    assert "exit 1" in text
    assert "claude|codex" in text


def test_review_round_caps_at_three_submitted_bot_reviews(workflow):
    job = _job(workflow, "review-round")
    run = next(step["run"] for step in job["steps"] if "run" in step)
    assert "github-actions[bot]" in run
    assert "submitted_at" in run
    assert "-lt 3" in run
    assert job["permissions"]["pull-requests"] == "read"


def test_claude_review_is_read_only(workflow):
    perms = _job(workflow, "claude-review")["permissions"]
    assert perms["pull-requests"] == "read"
    assert perms.get("contents") == "read"
    assert perms.get("id-token") != "write"


def test_codex_review_is_read_only(workflow):
    perms = _job(workflow, "codex-review")["permissions"]
    assert perms["pull-requests"] == "read"
    assert perms.get("contents") == "read"


def test_only_publisher_has_pull_request_write(workflow):
    writers = [
        name
        for name, job in workflow["jobs"].items()
        if (job.get("permissions") or {}).get("pull-requests") == "write"
    ]
    assert writers == ["post-review"]


def test_model_credentials_stay_in_matching_provider_jobs(workflow):
    for name, job in workflow["jobs"].items():
        text = _dump(job)
        if name == "claude-review":
            assert "ANTHROPIC_API_KEY" in text
            assert "OPENAI_API_KEY" not in text
        elif name == "codex-review":
            assert "OPENAI_API_KEY" in text
            assert "ANTHROPIC_API_KEY" not in text
        else:
            assert "ANTHROPIC_API_KEY" not in text
            assert "OPENAI_API_KEY" not in text


def test_post_review_checks_out_trusted_base_and_publisher(workflow):
    job = _job(workflow, "post-review")
    text = _dump(job)
    assert "github.event.pull_request.base.sha" in text
    assert "automated-review.js" in text
    assert job["permissions"]["pull-requests"] == "write"


def test_aggregate_check_is_named_automated_code_review(workflow):
    job = _job(workflow, "review-complete")
    assert job["name"] == "Automated Code Review"
    assert job.get("if") in ("${{ always() }}", "always()")


def test_no_second_independent_review_workflow():
    review_workflows = [
        path.name
        for path in WORKFLOWS_DIR.glob("*.yml")
        if "claude-code-action" in path.read_text()
        or "openai/codex-action" in path.read_text()
    ]
    assert review_workflows == ["automated-code-review.yml"]


def test_codex_action_is_pinned_and_read_only_sandbox():
    if not WORKFLOW_PATH.is_file():
        pytest.fail(f"missing {WORKFLOW_PATH}")
    text = WORKFLOW_PATH.read_text()
    assert re.search(r"openai/codex-action@[0-9a-f]{40}", text)
    assert "sandbox: read-only" in text


def test_prompt_and_schema_come_from_base_sha(workflow):
    text = _dump(_job(workflow, "codex-review"))
    assert "github.event.pull_request.base.sha" in text
    assert "prompt-file: trusted-review/.github/codex/prompts/review.md" in text
    assert (
        "output-schema-file: trusted-review/.github/schemas/review-findings.schema.json"
        in text
    )
    assert "persist-credentials: false" in text
    assert "wait-for-verify" not in workflow["jobs"]
    assert "fetch-verify-results" not in text


def test_codex_prompt_points_at_repo_conventions():
    if not PROMPT_PATH.is_file():
        pytest.fail(f"missing {PROMPT_PATH}")
    text = PROMPT_PATH.read_text()
    assert "CLAUDE.md" in text
    assert "UtcDateTime" in text
    assert "webapp/static/app.css" in text
    assert "templates/_ui.html" in text
    assert "verify-results.md" not in text


def test_publisher_script_and_schema_exist():
    assert PUBLISHER_PATH.is_file()
    assert SCHEMA_PATH.is_file()
