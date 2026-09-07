# Recording download

Source: `webapp/app.py:meeting_recording_download` (`GET /meetings/{id}/recording`),
`_mixed_recording`, `_download_stem`; button macro `recording_download` in
`webapp/templates/_ui.html` rendered by `meeting_detail.html` only when the file exists.

## Outcome

A user downloads the mixed MP3 of a meeting whose audio is on disk. The
button is absent, not disabled, when the file is missing.

## Reach it

Signed-in user; meeting with `asset_dir` containing `audio_mixed*.mp3`
(`seed.py recording`). The missing-file case needs a meeting that exists
without `asset_dir` (`seed.py transcript`).

## Recipe

```bash
uv run python $SKILL/scripts/seed.py recording
uv run python $SKILL/scripts/seed.py transcript   # has recording_id but no asset_dir
$SKILL/scripts/capture.sh rec-detail /meetings/bot-verify-recording -H 'accept: text/html'
$SKILL/scripts/capture.sh rec-download /meetings/bot-verify-recording/recording
$SKILL/scripts/capture.sh rec-missing /meetings/bot-verify-transcript/recording
```

## Success criteria

- `rec-detail.body` contains `href="/meetings/bot-verify-recording/recording"` with class `btn btn-line btn-sm`.
- `rec-download.status` 200, `content-type: audio/mpeg`,
  `content-disposition: attachment; filename="2026-08-02-Recording-Only.mp3"`, body bytes equal the seeded file.
- `rec-missing.status` 404 (meeting exists but no `asset_dir`); `/meetings/nope/recording` also 404.

## Gotchas

- Streams via `FileResponse`; large real files are fine, seeded bytes are tiny.
- Title with diacritics produces `filename*=utf-8''` in the header.

Evidence to save: the three captures.
