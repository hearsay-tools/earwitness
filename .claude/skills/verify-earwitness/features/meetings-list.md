# Meetings list

Source: `webapp/app.py:meetings_view` (`GET /meetings`), `webapp/app.py:api_meetings`
(`GET /api/meetings`), filters in `webapp/queries.py` (`MeetingFilters`,
`search_meetings`, `status_facets`), templates `webapp/templates/meetings.html`,
`_filters.html`, labels in `webapp/labels.py`, status axis `webapp/models.py:Meeting.user_status`.

## Outcome

A signed-in user sees finished meetings first, can widen to all, filter by
status / participants / dates, search by title, people, emails or ids, and
integrations read the same list as JSON.

Sub-features: default `view=finished` hides scheduled meetings; `view=all`;
`status=<user_status>` deep links; free-text `q`; sidebar counts per status;
pagination; sort; empty states with a way out.

## Reach it

Any signed-in user (dev user with `AUTH_DISABLED=1`). Needs at least one
meeting row; a fresh DB shows the empty state.

## Recipe

```bash
uv run python $SKILL/scripts/seed.py transcript
uv run python $SKILL/scripts/seed.py recording
$SKILL/scripts/capture.sh list-default /meetings -H 'accept: text/html'
$SKILL/scripts/capture.sh list-search '/meetings?q=Recording' -H 'accept: text/html'
$SKILL/scripts/capture.sh list-status '/meetings?status=ready' -H 'accept: text/html'
$SKILL/scripts/capture.sh list-empty '/meetings?q=nothingmatchesthis' -H 'accept: text/html'
$SKILL/scripts/capture.sh api-meetings /api/meetings
```

## Success criteria

- `list-default.status` is 200; body contains both titles `Verification Sync`
  and `Recording Only`, `data-label="Meeting"` cells and the "Show all meetings" toggle.
- `list-search.body` contains `Recording Only` and not `Verification Sync`.
- `list-status.body` (status=ready) contains `Verification Sync` only.
- `list-empty.body` contains `Nothing matches the filters` and `Clear filters`.
- `api-meetings.body` JSON has `total` 2, items with `user_status` `ready` and
  `to_process`, and `transcript_id` set only on the transcript meeting.

## Gotchas

- Unauthenticated `/api/meetings` is 401 JSON, HTML pages 302 to `/login`.
- The default view is *finished*; a meeting seeded with `status_group="scheduled"`
  is only visible with `view=all`.

Evidence to save: the five captures above.
