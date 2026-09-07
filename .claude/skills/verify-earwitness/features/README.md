# Earwitness feature map

Index of user-facing features with a live driving recipe each. Every recipe
assumes `source .claude/skills/verify-earwitness/scripts/env.sh <run-id>`
after `launch.sh`, and `SKILL=.claude/skills/verify-earwitness`.

| Feature | File | Live proof possible offline |
|---|---|---|
| Meetings list, default view, filters, search, JSON API | [meetings-list.md](meetings-list.md) | yes |
| Meeting detail and the transcript job queue | [meeting-detail-and-queue.md](meeting-detail-and-queue.md) | yes (queueing, listing, cancel); running the job needs keys |
| Transcript view and export formats | [transcript-view-and-export.md](transcript-view-and-export.md) | yes |
| Recording download | [recording-download.md](recording-download.md) | yes |
| Auth guard and health endpoint | [auth-guard-and-health.md](auth-guard-and-health.md) | guard yes; Google login needs OAuth client |
| Disk as source of truth: `repair_assets` adopts expired recordings | [repair-assets-from-disk.md](repair-assets-from-disk.md) | yes (real worker run) |

## Coverage still to add

- Recall sync (`POST /sync`, task `sync_recall` in `webapp/tasks.py`): needs `RECALL_API_KEY`.
- Transcription pipeline (`process` / `transcribe` tasks, `transcripts/energy_diarization.py`): needs `ELEVENLABS_API_KEY` and a real `audio_separate` asset set.
- Calendar titles and `POST /backfill-titles` (`webapp/gcal.py`): needs Google OAuth with `calendar.readonly`.
- Bulk actions `POST /meetings/bulk` and the transcripts list filters (`/transcripts`).
- Job retry `POST /jobs/{id}/retry` on a failed job; `cleanup_audio` task.
- CLI subcommands in `transcripts/cli.py`: `ground-truth` and `compare` run offline on fixture files; `pipeline-recall`, `transcribe`, `recall-fetch` need keys.
- Night theme and layout tokens in `webapp/static/app.css` (needs a browser).
