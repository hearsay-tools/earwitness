# Disk as source of truth: repair_assets adopts expired recordings

Source: task `repair_assets` in `webapp/tasks.py`; `adopt_local_recordings`,
`local_asset_state`, `adopt_disk_recording`, `_rec_dir_ready`,
`load_participants_from_disk` in `webapp/recall_sync.py`; worker entry
`webapp/worker.py` (`--once`); tests `tests/test_local_assets.py`.

## Outcome

A meeting Recall reports as expired (no `recording_id`) whose complete asset
set lies under `RECALL_DIR/<bot>/<rec>/` becomes processable: the worker
adopts the recording id from disk, sets `asset_state=ready`, `asset_dir`,
loads participants from `participants.json`, and the meeting moves from
"No recording" to "To process" in the UI.

## Reach it

Any run; the task is queued via the same `jobs.enqueue` the app uses
(there is no UI button for it; it is an operator task). No API keys needed.

## Recipe

```bash
uv run python $SKILL/scripts/seed.py expired-disk
$SKILL/scripts/capture.sh repair-before /meetings/bot-verify-expired -H 'accept: text/html'
uv run python $SKILL/scripts/seed.py enqueue repair_assets
uv run python -m webapp.worker --once 2> $VERIFY_EVIDENCE/worker-once.log; echo "exit=$?" >> $VERIFY_EVIDENCE/worker-once.log
echo $VERIFY_EVIDENCE/worker-once.log >> $VERIFY_EVIDENCE/manifest.txt
$SKILL/scripts/capture.sh repair-after /meetings/bot-verify-expired -H 'accept: text/html'
$SKILL/scripts/capture.sh repair-api "/api/meetings?q=Expired"
$SKILL/scripts/capture.sh repair-jobs '/api/jobs?limit=5'
uv run python $SKILL/scripts/seed.py dump > $VERIFY_EVIDENCE/repair-state.json; echo $VERIFY_EVIDENCE/repair-state.json >> $VERIFY_EVIDENCE/manifest.txt
```

## Success criteria

- `repair-before.body` has badge `b-no_recording` and no `Get transcript` button.
- `worker-once.log` ends with `job <id> (repair_assets) → done` and `exit=0`.
- `repair-after.body` has badge `b-to_process`, the `Get transcript` button, and
  the participant `Ala Testowa`.
- `repair-api.body` item: `asset_state` `ready`, `user_status` `to_process`,
  participants include `ala@example.com`.
- `repair-jobs.body` item has `kind` `repair_assets` and `status` `done` (the API
  omits `result`); `repair-state.json` has `jobs[0].result.adopted` 1 and the meeting
  with `recording_id` `rec-verify-3` and `asset_dir` inside the run's `RECALL_DIR`.

## Gotchas

- `_rec_dir_ready` needs `recording.json`, `audio_mixed*.mp3`, an
  `audio_separate` artifact with `format: raw`, its `parts_<id8>.json`
  manifest and at least one `*.raw`. Missing any one leaves the meeting expired.
- `worker --once` prints `kolejka pusta` and exits 0 when nothing is queued; check the log, not only the exit code.

Evidence to save: captures, `worker-once.log`, `repair-state.json`.
