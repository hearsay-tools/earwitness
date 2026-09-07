# Transcript view and export

Source: `webapp/app.py:transcripts_view` (`GET /transcripts`), `transcript_view`
(`GET /transcripts/{id}`), `transcript_download` (`GET /transcripts/{id}/download?fmt=`),
parsing and exporters in `webapp/tasks.py` (`parse_transcript`, `to_vtt`, `to_markdown`,
`transcript_text`), templates `webapp/templates/transcripts.html`, `transcript.html`;
speaker search in `webapp/static/app.js` (`data-transcript-search`, `data-speaker-filter`).

## Outcome

A user opens a finished transcript, sees utterances per speaker with
timestamps, filters by speaker, and downloads it as txt, md, vtt, json or
the raw ASR response, each with a filename built from date and title.

## Reach it

Signed-in user; a `Transcript` row whose `text_path` file exists
(`seed.py transcript` writes `<TRANSCRIPTS_DIR>/bot-verify-transcript/transcript.txt`).

## Recipe

```bash
TID=$(uv run python $SKILL/scripts/seed.py transcript | python3 -c 'import json,sys;print(json.load(sys.stdin)["transcript_id"])')
$SKILL/scripts/capture.sh transcripts-list /transcripts -H 'accept: text/html'
$SKILL/scripts/capture.sh transcript-view "/transcripts/$TID" -H 'accept: text/html'
for f in txt md vtt json raw; do $SKILL/scripts/capture.sh "download-$f" "/transcripts/$TID/download?fmt=$f"; done
$SKILL/scripts/capture.sh download-bad "/transcripts/$TID/download?fmt=doc"
```

## Success criteria

- `transcripts-list.body` shows `Verification Sync`, links `fmt=txt|md|vtt|json`, and no `fmt=raw`.
- `transcript-view.body` has three `class="utt"` blocks with `data-speaker="Ala Testowa"` /
  `"Bob Example"`, the speaker `<select data-speaker-filter>`, and every `fmt=` link including raw.
- `download-txt.body` equals the seeded text; header `content-disposition: attachment; filename="2026-08-01-Verification-Sync.txt"`.
- `download-md.body` starts with `# Verification Sync` and lists participants.
- `download-vtt.body` starts with `WEBVTT` and has `<v Ala Testowa>` cues.
- `download-json.body` parses; `transcript.utterances` has 3 items; `meeting.participants` carries `speaking_seconds`.
- `download-raw.status` 404 (seed has no `raw_path`); `download-bad.status` 400.

## Gotchas

- Delete the text file and the view answers 410 with the branded "The file is gone" page.
- Filenames with diacritics are RFC 5987 encoded (`filename*=utf-8''...`).

Evidence to save: all captures above.
