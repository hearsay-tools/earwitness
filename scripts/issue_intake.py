"""Automated triage of newly opened issues (issue-intake workflow).

`analyze` — read-only: compare a new issue against open issues and the repo's
area taxonomy with an LLM, write the verdict to a JSON artifact.
`apply` — holds `issues: write`: apply exactly one existing area label,
report a likely duplicate, close on high confidence. Mutations are pinned to
the issue number that triggered the run; a label that does not exist in the
repo is never applied or created.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

AREA_LABELS = ("pipeline", "recall", "webapp", "infra", "docs", "process")
DUPLICATE_LABEL = "duplicate"
DEFAULT_MODELS = {"claude": "claude-sonnet-4-5", "codex": "gpt-5.6-luna"}
MAX_OPEN_ISSUES = 200
RESULT_FILE = "intake-result.json"

SYSTEM_PROMPT = (
    "You triage issues in a small software repo. Pick exactly one area label "
    "for the new issue from the provided list — never invent a label. Then "
    "decide whether the new issue duplicates one of the open issues listed. "
    "Use high confidence only when both issues describe the same underlying "
    "problem. Reply with a single JSON object and nothing else: "
    '{"area_label": "<name>", "duplicate": null | {"number": <int>, '
    '"confidence": "high"|"low", "reason": "<one short sentence>"}}'
)


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def area_candidates(repo_labels: list[dict]) -> list[dict]:
    by_name = {label["name"]: label for label in repo_labels}
    return [by_name[name] for name in AREA_LABELS if name in by_name]


def parse_model_json(raw: str | None) -> dict | None:
    text = raw or ""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_result(
    parsed: dict | None, candidate_names: list[str], open_numbers: set[int]
) -> dict:
    if not isinstance(parsed, dict):
        raise ValueError("model did not return a JSON object")

    label = parsed.get("area_label")
    if isinstance(label, str) and label.strip().lower() in candidate_names:
        label = label.strip().lower()
    else:
        raise ValueError(f"model returned invalid area_label: {label!r}")

    duplicate = None
    raw_dup = parsed.get("duplicate")
    if isinstance(raw_dup, dict):
        number = raw_dup.get("number")
        if (
            isinstance(number, int)
            and not isinstance(number, bool)
            and number in open_numbers
        ):
            confidence = str(raw_dup.get("confidence", "low")).strip().lower()
            if confidence not in ("high", "low"):
                confidence = "low"
            duplicate = {
                "number": number,
                "confidence": confidence,
                "reason": truncate(str(raw_dup.get("reason", "")), 280),
            }
        else:
            print(
                f"::warning title=duplicate::ignoring duplicate {number!r}: "
                "not an open issue in this repo"
            )
    return {"area_label": label, "duplicate": duplicate}


def duplicate_comment(
    target_url: str, target_number: int, reason: str, closing: bool
) -> str:
    lines = [
        f"Automated triage (issue-intake): likely duplicate of "
        f"#{target_number} ({target_url})."
    ]
    if reason:
        lines.append(f"> {reason}")
    lines.append(
        "Closing automatically as a likely duplicate — reopen if this is wrong."
        if closing
        else "Flagging only; leaving open for a human decision."
    )
    return "\n".join(lines)


def plan_apply(
    result: dict,
    *,
    trigger_number: int,
    live_labels: set[str],
    duplicate_target: dict | None,
) -> dict:
    if result.get("issue_number") != trigger_number:
        raise ValueError(
            f"result is for issue #{result.get('issue_number')}, but this run "
            f"was triggered by #{trigger_number}; refusing to act"
        )
    label = result.get("area_label")
    if label not in live_labels:
        raise ValueError(
            f"label {label!r} does not exist in this repo; refusing to apply or create it"
        )

    duplicate = result.get("duplicate")
    target_usable = (
        duplicate is not None
        and duplicate_target is not None
        and duplicate_target.get("state") == "open"
    )
    closing = bool(target_usable and duplicate.get("confidence") == "high")

    add_labels = [label]
    if closing and DUPLICATE_LABEL in live_labels:
        add_labels.append(DUPLICATE_LABEL)

    comment = None
    if target_usable:
        comment = duplicate_comment(
            duplicate_target["html_url"],
            duplicate["number"],
            duplicate.get("reason", ""),
            closing,
        )
    return {"add_labels": add_labels, "comment": comment, "close": closing}


def build_prompt(issue: dict, candidates: list[dict], open_issues: list[dict]) -> str:
    label_lines = "\n".join(
        f"- {c['name']}: {c.get('description') or '(no description)'}"
        for c in candidates
    )
    open_lines = "\n".join(
        f"- #{i['number']} {i['title']}\n  {truncate(i.get('body') or '', 300)}"
        for i in open_issues
    )
    return f"""## New issue to triage
#{issue["number"]} {issue["title"]}
{truncate(issue.get("body") or "", 2000)}

## Area labels (pick exactly one, by the boundary each description states)
{label_lines or "(none)"}

## Open issues (the only valid duplicate candidates)
{open_lines or "(none)"}

Return only the JSON object described above.
"""


def gh_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )


def gh_get(client: httpx.Client, path: str, params: dict | None = None):
    response = client.get(path, params=params)
    response.raise_for_status()
    return response.json()


def fetch_issue(client: httpx.Client, repo: str, number: int) -> dict:
    return gh_get(client, f"/repos/{repo}/issues/{number}")


def fetch_labels(client: httpx.Client, repo: str) -> list[dict]:
    labels: list[dict] = []
    page = 1
    while True:
        batch = gh_get(client, f"/repos/{repo}/labels", {"per_page": 100, "page": page})
        labels.extend(batch)
        if len(batch) < 100 or len(labels) >= 500:
            return labels
        page += 1


def fetch_open_issues(
    client: httpx.Client, repo: str, exclude_number: int
) -> list[dict]:
    issues: list[dict] = []
    page = 1
    while len(issues) < MAX_OPEN_ISSUES:
        batch = gh_get(
            client,
            f"/repos/{repo}/issues",
            {"state": "open", "per_page": 100, "page": page},
        )
        issues.extend(
            i
            for i in batch
            if "pull_request" not in i and i["number"] != exclude_number
        )
        if len(batch) < 100:
            break
        page += 1
    return issues[:MAX_OPEN_ISSUES]


def ask_claude(prompt: str, model: str) -> str:
    import anthropic

    message = anthropic.Anthropic().messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )


def ask_codex(prompt: str, model: str) -> str:
    from openai import OpenAI

    completion = OpenAI().chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        reasoning_effort="medium",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    return completion.choices[0].message.content or ""


def run_model(
    provider: str,
    prompt: str,
    model: str,
    candidate_names: list[str],
    open_numbers: set[int],
) -> dict:
    ask = ask_claude if provider == "claude" else ask_codex
    error: ValueError = ValueError("model did not return a usable verdict")
    for attempt in (1, 2):
        suffix = "\n\nReturn ONLY the JSON object." if attempt == 2 else ""
        parsed = parse_model_json(ask(prompt + suffix, model))
        if parsed is None:
            error = ValueError("model did not return parseable JSON")
            continue
        try:
            return normalize_result(parsed, candidate_names, open_numbers)
        except ValueError as exc:
            error = exc
    raise error


def cmd_analyze(args: argparse.Namespace) -> int:
    with gh_client(args.token) as client:
        issue = fetch_issue(client, args.repo, args.issue)
        repo_labels = fetch_labels(client, args.repo)
        open_issues = fetch_open_issues(client, args.repo, args.issue)

    candidates = area_candidates(repo_labels)
    if not candidates:
        raise SystemExit("no area labels exist in this repo; refusing to guess")

    prompt = build_prompt(issue, candidates, open_issues)
    result = run_model(
        args.provider,
        prompt,
        args.model or DEFAULT_MODELS[args.provider],
        [c["name"] for c in candidates],
        {i["number"] for i in open_issues},
    )
    payload = {
        "issue_number": args.issue,
        "provider": args.provider,
        "area_label": result["area_label"],
        "duplicate": result["duplicate"],
    }
    Path(args.result_path).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"analysis: {json.dumps(payload)}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    result = json.loads(Path(args.result_path).read_text())
    with gh_client(args.token) as client:
        live_labels = {label["name"] for label in fetch_labels(client, args.repo)}
        duplicate_target = None
        if result.get("duplicate"):
            try:
                duplicate_target = fetch_issue(
                    client, args.repo, result["duplicate"]["number"]
                )
            except httpx.HTTPError as exc:
                print(
                    f"::warning title=duplicate::cannot fetch duplicate target: {exc}"
                )
        plan = plan_apply(
            result,
            trigger_number=args.issue,
            live_labels=live_labels,
            duplicate_target=duplicate_target,
        )
        issue_path = f"/repos/{args.repo}/issues/{args.issue}"
        if args.dry_run:
            print(f"dry-run: would execute on #{args.issue}: {json.dumps(plan)}")
            return 0
        if plan["add_labels"]:
            client.post(
                issue_path + "/labels", json={"labels": plan["add_labels"]}
            ).raise_for_status()
        if plan["comment"]:
            client.post(
                f"/repos/{args.repo}/issues/{args.issue}/comments",
                json={"body": plan["comment"]},
            ).raise_for_status()
        if plan["close"]:
            client.patch(issue_path, json={"state": "closed"}).raise_for_status()
    print(f"applied: {json.dumps(plan)}")
    return 0


def env_default(name: str) -> str | None:
    return os.environ.get(name) or None


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"issue number must be an integer: {value!r}"
        ) from None
    if number <= 0:
        raise argparse.ArgumentTypeError("issue number must be positive")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=env_default("GITHUB_REPOSITORY"))
    common.add_argument(
        "--issue",
        default=env_default("ISSUE_NUMBER"),
        help="defaults to $ISSUE_NUMBER; every mutation targets this issue only",
    )
    common.add_argument(
        "--token", default=env_default("GH_TOKEN") or env_default("GITHUB_TOKEN")
    )

    analyze = sub.add_parser("analyze", parents=[common])
    analyze.add_argument(
        "--provider",
        choices=sorted(DEFAULT_MODELS),
        default=env_default("ISSUE_INTAKE_PROVIDER"),
    )
    analyze.add_argument("--model", default=env_default("ISSUE_INTAKE_MODEL"))
    analyze.add_argument("--result-path", default=RESULT_FILE)
    analyze.set_defaults(func=cmd_analyze)

    apply = sub.add_parser("apply", parents=[common])
    apply.add_argument("--result-path", default=RESULT_FILE)
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(func=cmd_apply)

    args = parser.parse_args(argv)
    if not getattr(args, "issue", None):
        sys.exit("error: --issue or $ISSUE_NUMBER is required")
    args.issue = positive_int(args.issue)
    if not args.repo:
        sys.exit("error: --repo or $GITHUB_REPOSITORY is required")
    if not args.token:
        sys.exit("error: --token or $GH_TOKEN is required")
    if args.command == "analyze" and not args.provider:
        sys.exit("error: --provider or $ISSUE_INTAKE_PROVIDER is required")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
