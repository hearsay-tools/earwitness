# Peck adapter: Earwitness

Earwitness is a FastAPI + Jinja2 web app (server-rendered HTML, SQLite in this setup). The scripts
in this folder start one isolated instance per run, seed a spec's Given tables, and sign a person in.
All commands run from the repository root.

## 1. Start

Install once per checkout (Python 3.12 and `uv` must be on the PATH):

```bash
uv sync
```

Start one run in the foreground:

```bash
PECK_DATA=/tmp/peck-run-1 PECK_PORT=8811 peck/start.sh
```

- `peck/start.sh` sources `peck/env.sh` and runs
  `uv run uvicorn webapp.app:app --host 127.0.0.1 --port $PECK_PORT`.
- Up when `GET http://127.0.0.1:$PECK_PORT/healthz` answers 200 with `{"ok":true,...}`.
- Base URL for the browser: `http://127.0.0.1:$PECK_PORT`. Use `127.0.0.1`, not `localhost`, so
  the session cookie (below) matches.
- Start only the web process. Do **not** start the job worker (`python -m webapp.worker`, `./dev.sh`).
  See Gotchas.

## 2. Fresh data

Everything a run stores lives under `PECK_DATA`:

| Path | What |
|---|---|
| `$PECK_DATA/webapp.db` | SQLite database (`DATABASE_URL`) |
| `$PECK_DATA/recall/` | fake recording files (`RECALL_DIR`) |
| `$PECK_DATA/transcripts/` | transcript text files (`TRANSCRIPTS_DIR`) |

Give every run a new, empty directory (or `rm -rf` it first). Nothing is shared between runs; the
developer database `output/webapp.db` is never touched because `peck/env.sh` overrides
`DATABASE_URL`. The schema is created on first use (by the seeder or by the server).

## 3. Seeding

Seed **before** starting the server (seeding a running server also works; pages read the DB on
every request):

```bash
export PECK_DATA=/tmp/peck-run-1 PECK_PORT=8811
source peck/env.sh
uv run python peck/seed.py peck/spec/get-a-transcript.md
```

`peck/seed.py` reads the spec's `## Given` tables itself and writes them through the app's models.
It prints the row count per table. Mapping:

### People → `users`

| Column | Model field |
|---|---|
| Name | `User.name` (shown in the page header) |
| Role | not stored; product-owner context only |
| Email | `User.email` (shown as "started by" on jobs) |

There are no passwords: the app signs people in with Google only. See "Signing in".

### Meetings → `meetings`, `meeting_participants`, `transcripts`

| Column | Model field |
|---|---|
| Meeting | `Meeting.title`; the meeting id is `bot-` + the title slugged (`Harbor kickoff` → `/meetings/bot-harbor-kickoff`) |
| Date, Start | `Meeting.started_at` (UTC). Upcoming meetings get `join_at` only |
| Minutes | `Meeting.duration_seconds` / `completed_at` |
| Platform | `Meeting.platform`: `Google Meet`, `Zoom`, `Microsoft Teams`, `Webex`, `Slack Huddle` |
| Status | the visible status, mapped to the underlying fields (below) |
| Participants | comma-separated names → one `MeetingParticipant` each; email from the People table, else `first.last@example.com` |

| Status | What the seeder creates |
|---|---|
| To process | finished meeting with a recording id and a fake `audio_mixed.mp3` on disk, no transcript |
| Ready | as above plus a 3-line transcript file and a `Transcript` row |
| No recording | finished meeting, no recording |
| Failed | meeting whose bot failed (`status_group=failed`) |
| Upcoming | scheduled meeting, not started |

### Jobs → `jobs`

| Column | Model field |
|---|---|
| Meeting | the meeting with that title |
| Job | `Job.kind` by its visible label (`Download + transcription` → `process`, see `webapp/labels.py`) |
| Status | `Job.status` by its visible label: `Waiting`, `In progress`, `Done`, `Failed`, `Canceled` |
| Started by | a People name → that person's email in `Job.created_by` |

Seeded jobs are created on 2026-09-01 from 09:00 UTC, one minute apart, in table order. A `Failed`
job gets attempt 3 of 3 and the error "Recording is not available any more".

### Webhook → `app_settings`

| Column | Key |
|---|---|
| URL | `webhook_url` |
| Method | `webhook_method` (`POST` or `GET`) |
| Bearer token | `webhook_bearer_token` |

### Signing in ("X is signed in")

The real sign-in is Google OAuth, which cannot run offline. The sign-in guard stays **on**
(`AUTH_DISABLED` is empty); the verifier signs a person in by setting the app's own session cookie:

```bash
uv run python peck/seed.py --cookie "Marta Lis"     # same env as above, after seeding
```

It prints the value of a Starlette session cookie, signed with the run's `SECRET_KEY`, that holds
that person's user id. Add it to the browser context before the first page load:

| Field | Value |
|---|---|
| name | `session` |
| value | output of the command |
| url | `http://127.0.0.1:$PECK_PORT` |

The cookie is valid for 14 days and survives an app restart. Without it every page redirects to
`/login`.

## 4. Restart

`transcript-webhook.md` has "**The app:** restarts".

1. Stop the server: send SIGTERM to the process group of `peck/start.sh` (it runs `uv`, which runs
   uvicorn). Wait until the port is free.
2. Start it again with the same `PECK_DATA` and `PECK_PORT`: `peck/start.sh`.
3. Wait for `/healthz`. Data and the session cookie stay valid; do not re-seed.

## 5. Needs

- **Tools:** Python 3.12, `uv`. Nothing else (no Docker, no Redis, no Node).
- **Environment:** set by `peck/env.sh`; the caller only provides `PECK_DATA` and `PECK_PORT`.
  It also blanks every external key (`RECALL_API_KEY`, `ELEVENLABS_API_KEY`, `GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET`, `TRANSCRIPT_API_TOKEN`, Honcho), so a developer's `.env` cannot leak in.
  `config.py` loads `.env` without override, so these exports win.
- **External services:** none are called by the three specs. Recall.ai, ElevenLabs, Google Calendar
  and the webhook target are only reached by background jobs, and no worker runs.
- **Startup:** about 2–3 s to `/healthz` on a warm `uv` cache; the first `uv run` after a clean
  checkout also installs dependencies (`uv sync` up front avoids that).
- **Time:** dates and times are rendered in the server's local time zone. `peck/env.sh` pins
  `TZ=UTC`, so a meeting seeded at `10:00` shows as `10:00`. Job ages ("just now", "66 days ago")
  depend on the clock; no criterion asks about them.

## 6. Gotchas

- **No worker.** Jobs stay "Waiting" because nothing picks them up. That is what
  `get-a-transcript.md` relies on. With a worker and no API keys, the "Download + transcription"
  job fails within seconds and the counts on the Queue page change.
- The Queue page shows the notice "Jobs are waiting, but nothing is picking them up." whenever a job
  waits and no worker runs. It is expected here and no criterion depends on it.
- **Do not press** "Sync with Recall.ai" or "Fetch N missing titles" on the Meetings page. They queue
  background jobs that need Recall.ai or Google and change the Queue counts.
- The meeting and Queue pages poll `/api/jobs` every few seconds to update active job rows. The
  values do not change without a worker, so this does not make runs differ.
- After a save the Settings page URL is `/settings?saved=1` (or `?error=invalid_url`). Reloading it
  shows the same notice again; the form values always come from the database.
- The Webhook URL field is `type="url"`: the browser itself refuses text that is not a URL before
  the app sees it. Refusals in the specs use well-formed URLs so the app's own message appears.
- The meetings list hides "Upcoming" meetings by default (finished meetings only). A spec that needs
  them ticks the "Upcoming" status filter.
- Every run's job numbers start at 1 because the database is new; specs do not depend on them.
