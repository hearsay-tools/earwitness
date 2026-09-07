# Meeting detail and the transcript job queue

Source: `webapp/app.py:meeting_detail` (`GET /meetings/{id}`),
`meeting_enqueue` (`POST /meetings/{id}/enqueue`), `jobs_view` (`GET /jobs`),
`job_cancel`, `job_retry`, `api_jobs` (`GET /api/jobs?ids=`), `api_job_log`;
queue contract in `webapp/jobs.py` (`enqueue` dedupe by `kind:meeting_id`,
`cancel`, `queue_stats`); template `webapp/templates/meeting_detail.html`, `jobs.html`;
live polling in `webapp/static/app.js` (`data-job-id`, `data-job-active`).

## Outcome

From a finished meeting with a recording, one click "Get transcript" queues
a `process` job. The user is redirected back with `?job=<id>`, the job shows
in the meeting's job table and on `/jobs`, the nav counter updates, polling
JSON exposes progress and labels, and the job can be cancelled.

## Reach it

Signed-in user; a meeting with `recording_id` set and `transcript_state != ready`
(`seed.py recording`).

## Recipe

```bash
uv run python $SKILL/scripts/seed.py recording
$SKILL/scripts/capture.sh detail-before /meetings/bot-verify-recording -H 'accept: text/html'
$SKILL/scripts/capture.sh enqueue /meetings/bot-verify-recording/enqueue -X POST -d kind=process
JOB=$(grep -io 'job=[0-9]*' $VERIFY_EVIDENCE/enqueue.headers | cut -d= -f2)
$SKILL/scripts/capture.sh enqueue-dup /meetings/bot-verify-recording/enqueue -X POST -d kind=process
$SKILL/scripts/capture.sh detail-after "/meetings/bot-verify-recording?job=$JOB" -H 'accept: text/html'
$SKILL/scripts/capture.sh jobs-page /jobs -H 'accept: text/html'
$SKILL/scripts/capture.sh api-jobs "/api/jobs?ids=$JOB"
$SKILL/scripts/capture.sh job-log "/api/jobs/$JOB/log"
$SKILL/scripts/capture.sh healthz /healthz
$SKILL/scripts/capture.sh cancel "/jobs/$JOB/cancel" -X POST
$SKILL/scripts/capture.sh api-jobs-cancelled "/api/jobs?ids=$JOB"
uv run python $SKILL/scripts/seed.py dump > $VERIFY_EVIDENCE/state-after.json; echo $VERIFY_EVIDENCE/state-after.json >> $VERIFY_EVIDENCE/manifest.txt
```

## Success criteria

- `detail-before.body` has the `Get transcript` button, no `name="force"`, badge `b-to_process`.
- `enqueue.status` 303 with `location: /meetings/bot-verify-recording?job=<id>`.
- `enqueue-dup.headers` redirect to the **same** job id (dedupe key `process:bot-verify-recording`).
- `detail-after.body` has `data-job-id="<id>"` with `data-job-active`, label
  `Download + transcription`, badge `b-processing` (transcript_state became `queued`).
- `jobs-page.body` lists the job; `api-jobs.body` item has `status` `queued`,
  `kind_label` `Download + transcription`, `status_label` from `labels.py`.
- `healthz.body` `queue.queued` is 1. `job-log.body` is `(no logs)` before a worker touches it.
- `cancel.status` 303; `api-jobs-cancelled.body` status `canceled`; `state-after.json`
  shows the meeting back to `transcript_state` `none` (`release_meeting_state`).

## Gotchas

- Without a worker the job stays `queued` forever, which is what this recipe wants.
- With `launch.sh --worker` and no `RECALL_API_KEY`, `process` fails fast and
  retries with backoff; `attempts` grows and `error` is set. That is the
  real behaviour, not a bug in the recipe.
- Running the job to completion needs `RECALL_API_KEY` + `ELEVENLABS_API_KEY`
  and spends money. Unresolved prerequisite in this checkout.

Evidence to save: all captures above plus `state-after.json`.
