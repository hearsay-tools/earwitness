---
name: verify-earwitness
description: Launch and drive the real Earwitness webapp (FastAPI meeting-transcription PoC in webapp/) in an isolated instance to prove a change works end to end. Entry points are the HTML pages /meetings, /meetings/{id}, /transcripts, /transcripts/{id}, /jobs, the JSON API /api/meetings, /api/jobs, /api/jobs/{id}/log, /healthz, transcript and recording downloads, and the DB-backed job queue run by webapp.worker. Invoke after changing anything under webapp/ (routes, templates, queue, tasks, labels, static) or when asked to run, start, or confirm the app works, not just the tests.
---

# Verify Earwitness

Drive the real webapp over HTTP against an isolated instance. No browser
tooling is installed in the repo, so pages are fetched with `curl` and
asserted on their HTML, headers and JSON. The transcript worker runs as a
separate process on the same database; the pipeline itself needs paid
external keys (see Limitations).

All paths below are relative to the repository root. `SKILL=.claude/skills/verify-earwitness`.

## Launch

Dependencies: `uv` (Python 3.12, `uv sync` happens implicitly on first
`uv run`), `curl`, `python3`, `ss`. No Docker, no browser, no `.env` needed.

```bash
SKILL=.claude/skills/verify-earwitness
$SKILL/scripts/launch.sh                 # web only; prints run id + URL
$SKILL/scripts/launch.sh --worker        # web + one worker process
source $SKILL/scripts/env.sh <run-id>    # load the run's env into this shell
```

What launch does (`scripts/launch.sh`, `scripts/env.sh`):

- Isolated state under `output/verify/<run-id>/`: own SQLite file
  (`DATABASE_URL`), own `RECALL_DIR` and `TRANSCRIPTS_DIR`, a free
  loopback port, `AUTH_DISABLED=1` (dev user `dev@localhost`, see
  `webapp/auth.py:dev_user`), `AUTOSYNC_INTERVAL=0`, `AUTOPROCESS` off.
  `webapp/config.py` loads `.env` without override, so these exports win.
- Starts `uv run uvicorn webapp.app:app --host 127.0.0.1 --port <port>`
  (no `--reload`; restart after code changes). Pid in `web.pid`, log in `web.log`.
- Readiness: `GET /healthz` returns `{"ok":true,"queue":{...}}` (`webapp/app.py:healthz`).
- Ownership: the run owns only the processes whose pids sit in its state
  dir. A developer's `./dev.sh` on port 8000 is never touched.

Run `source $SKILL/scripts/env.sh <run-id>` in every new shell before
using `seed.py`, `capture.sh`, or the worker, otherwise they hit the
default `output/webapp.db`. `seed.py` refuses to run against it.

## Doctor

```bash
$SKILL/scripts/doctor.sh <run-id>
```

Read-only. Checks the state dir, that the pid is alive and is uvicorn
bound to this run's port, `/healthz`, that HEAD has not moved since
launch, the auth mode (`/meetings` gives 200 with auth disabled, 302 to
`/login` with auth on), and whether `RECALL_API_KEY`, `ELEVENLABS_API_KEY`,
`GOOGLE_CLIENT_ID` are available. Run it before driving and after any
surprising response. Exit code 0 means drive.

## Drive

Seed data first; a fresh instance has no meetings (`GET /meetings` renders
the empty state with a link to `/sync`). Scenarios mirror the fixtures
in `tests/test_views.py` and `tests/test_local_assets.py`:

```bash
uv run python $SKILL/scripts/seed.py transcript     # meeting + ready transcript file
uv run python $SKILL/scripts/seed.py recording      # meeting + audio_mixed.mp3, no transcript
uv run python $SKILL/scripts/seed.py expired-disk   # recording_id NULL, full asset set on disk
uv run python $SKILL/scripts/seed.py enqueue repair_assets   # queue a task via jobs.enqueue
uv run python $SKILL/scripts/seed.py dump           # JSON snapshot of meetings + jobs
```

Then take the user path over HTTP. `capture.sh` saves each exchange as
evidence and prints the status; relative paths resolve to the run's URL.
Redirects are not followed so 303 targets stay visible:

```bash
$SKILL/scripts/capture.sh meetings /meetings -H 'accept: text/html'
$SKILL/scripts/capture.sh detail /meetings/bot-verify-recording -H 'accept: text/html'
$SKILL/scripts/capture.sh enqueue /meetings/bot-verify-recording/enqueue -X POST -d kind=process
$SKILL/scripts/capture.sh api-jobs '/api/jobs?ids=1'
$SKILL/scripts/capture.sh download '/transcripts/1/download?fmt=vtt'
```

Stable handles in the HTML (from `webapp/templates/`): forms post to
`/meetings/{id}/enqueue` with `name=kind`, `/jobs/{id}/cancel`,
`/jobs/{id}/retry`, `/sync`, `/backfill-titles`; job rows carry
`data-job-id` and `data-job-active`; utterances are `div.utt[data-speaker]`;
badges are `span.badge.b-<user_status>`; table cells carry `data-label`.
Labels come from `webapp/labels.py`, not the templates.

Worker: with `launch.sh --worker` queued jobs run automatically. Without
it, run exactly one job in the foreground and see its outcome:

```bash
uv run python -m webapp.worker --once     # exit 0 = job done, 1 = failed, "kolejka pusta" = nothing queued
```

Feature recipes with success criteria live in `features/README.md`.

## Evidence

Everything goes to `.verification-evidence/<run-id>/` at the repo root
(gitignored, survives cleanup). `capture.sh <name> <path>` writes
`<name>.status`, `<name>.headers`, `<name>.body` and appends them to
`manifest.txt`. For stored-state side effects save a `seed.py dump`:

```bash
uv run python $SKILL/scripts/seed.py dump > $VERIFY_EVIDENCE/state-after.json
echo $VERIFY_EVIDENCE/state-after.json >> $VERIFY_EVIDENCE/manifest.txt
```

`cleanup.sh` copies `web.log` and `worker.log` into the evidence dir.
Seeded data is fictional; never copy real bot ids, names or transcripts
from `output/` into evidence.

## Cleanup

```bash
$SKILL/scripts/cleanup.sh <run-id>
```

Sends TERM (then KILL) only to the pids recorded in this run's state dir
after checking their cmdline is uvicorn / `webapp.worker`. Any other
process still listening on the run's port is reported and left untouched,
since it cannot be proven to be ours. It then archives logs, removes `output/verify/<run-id>/`,
then re-checks every path in `manifest.txt` and exits 1 if any is missing.
Clean failed attempts the same way; their captures are still proof.

## Reset a wedged instance

Doctor green but the user path still fails (e.g. `/api/jobs` shows a job
`running` forever, or SQLite `database is locked`):

1. `$SKILL/scripts/cleanup.sh <run-id>` (evidence stays).
2. `$SKILL/scripts/launch.sh <new-run-id>` and re-seed. State is per run;
   there is nothing shared to repair.
3. If the port is still bound, `ss -ltnp 'sport = :<port>'` names the
   owner. Kill it only if it is a `uv run uvicorn` you started.

Stale jobs: the worker's scheduler `reap_stale()` runs only in the
long-lived worker (`launch.sh --worker`), after `JOB_STALE_AFTER` seconds
(default 900). For a quick reset start a new run instead of waiting.

## Helpers

All inside this directory; make them executable after checkout
(`chmod +x $SKILL/scripts/*.sh`).

| Helper | Invocation | Purpose |
|---|---|---|
| `scripts/env.sh` | `source scripts/env.sh [run-id]` | exports the run's isolated env (port, DB, dirs, auth flags) |
| `scripts/launch.sh` | `scripts/launch.sh [--worker] [run-id]` | start web (+ worker), wait for `/healthz` |
| `scripts/doctor.sh` | `scripts/doctor.sh [run-id]` | read-only readiness and identity checks |
| `scripts/seed.py` | `uv run python scripts/seed.py <scenario>` | synthetic meetings, transcripts, disk assets, jobs; `dump` snapshot |
| `scripts/capture.sh` | `scripts/capture.sh <name> <path> [curl args]` | save status/headers/body as evidence |
| `scripts/cleanup.sh` | `scripts/cleanup.sh [run-id]` | stop own processes, drop state, verify evidence |

`VERIFY_AUTH=google scripts/launch.sh` starts the instance with the real
auth guard (`AUTH_DISABLED` empty) to prove redirects and 401s; the
Google login itself needs `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`. Launch
persists the mode in `output/verify/<run-id>/auth`, so `doctor.sh` and
`capture.sh` in another shell read the mode the server runs with.

Run ids are one path segment (`[A-Za-z0-9._-]+`); `env.sh` rejects anything
else because cleanup removes `output/verify/<run-id>` recursively.

## Limitations and unresolved prerequisites

- **No API keys in this checkout** (`.env` absent). `sync_recall`,
  `fetch_assets`, `transcribe`, `process`, `sync_calendar` and
  `backfill_titles` fail or stay queued without `RECALL_API_KEY`,
  `ELEVENLABS_API_KEY` and a Google OAuth client. With keys in `.env` they
  flow into the run unchanged; `process` then spends ElevenLabs credit.
- **No browser**: JS behaviours in `webapp/static/app.js` (live job
  polling, dialog confirm, transcript search) are verified by the
  endpoints they call (`/api/jobs?ids=`, `/api/jobs/{id}/log`) and the
  `data-*` hooks they need, not by executing the script.
- **Google login** is a redirect to accounts.google.com; only the guard
  (302 to `/login`, 401 JSON on `/api/*`) is provable offline.
