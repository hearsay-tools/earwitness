# User-facing meeting status axis — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One derived, user-facing `user_status` axis (7 statuses) replaces the Recall-lifecycle `status_group` + `transcript_state` filter pair everywhere in the UI, with a "Finished" default view.

**Architecture:** Derivation lives in `webapp/models.py` as a Python property plus an equivalent SQL `case()` expression (`user_status_case(now)`), so the same rules filter, facet and sort in `queries.py`. Labels/badge classes map the axis to UI copy. The HTTP layer (`app.py`) seeds the default view; templates render it. No schema change — `status_group` stays stored as internal state.

**Tech Stack:** FastAPI + Jinja2 + SQLAlchemy 2.0, server-side rendered, zero build step. Tests: `uv run pytest`.

**Spec:** GitHub issue #1 (wjarka/earwitness) — derivation table, 7 statuses, one axis. **One deviation, approved by the repo owner in the design session:** the default view is named **"Finished"** (not "Needs attention") and consists of `ready + to_process + failed + no_recording` — i.e. it *includes* Ready meetings; only the machine-owned live states (`upcoming`, `in_meeting`, `processing`) are excluded.

## Global Constraints

- Everything runs through `uv` (`uv run pytest`, `uv run python main.py …`) — never pip/global installs.
- UI copy is English; code comments in this repo are Polish — keep both conventions.
- Visual layer rides on tokens from `webapp/static/app.css` — no raw hexes or ms in components; reuse existing `--*-soft`/`--*-line` color vars for new badge classes.
- Raw Recall `status_code` / `status_sub_code` stay visible in tooltips and the detail "Status in Recall" row; `STATUS_HINTS` still explain failures.
- No changes to Recall sync, job queue, or pipeline logic; `meetings_ready_to_process()` stays as-is (the derivation mirrors it, it does not replace it).
- No DB migration: `user_status` is derived at runtime.
- Disk is the source of truth: `status_group == "expired"` with assets on disk derives to `to_process`, never `no_recording`.
- Datetimes compared against `utcnow()` come back from DB via `UtcDateTime` (already the case for `media_expires_at`).

## Statuses (display + rank order = lifecycle)

| key | label | badge color family |
|---|---|---|
| `upcoming` | Upcoming | gray |
| `in_meeting` | In meeting | blue |
| `processing` | Processing | blue |
| `to_process` | To process | amber |
| `ready` | Ready | green |
| `failed` | Failed | red |
| `no_recording` | No recording | gray |

Derivation precedence (first match wins — issue #1 verbatim, with `to_process` transcript guard `== "none"` because a failed transcript already matched `failed` above):

1. `ready` — `transcript_state == "ready"`
2. `upcoming` — `status_group == "scheduled"`
3. `in_meeting` — `status_group in ("joining", "recording")`
4. `failed` — `status_group == "failed"` OR `transcript_state == "failed"` OR `asset_state == "failed"`
5. `processing` — `asset_state in ("queued", "fetching")` OR `transcript_state in ("queued", "running")`
6. `to_process` — `status_group in ("done", "expired")` AND `transcript_state == "none"` AND (`asset_state == "ready"` OR (`recording_id` set AND (`media_expires_at` is None OR `> now`)))
7. `no_recording` — else

Default view: `DEFAULT_VIEW_STATUSES = ("to_process", "ready", "failed", "no_recording")`, label "Finished", toggle link "Show all meetings" / "Show finished only".

---

### Task 1: derivation core in `models.py`

**Files:**
- Modify: `webapp/models.py` (import `and_, Case, case, or_` from sqlalchemy; add constants + property + SQL expression near `status_group()`)
- Test: `tests/test_user_status.py` (new)

**Interfaces:**
- Produces: `USER_STATUS_ORDER: tuple[str, ...]`, `DEFAULT_VIEW_STATUSES: tuple[str, ...]`, `Meeting.user_status -> str` (property), `user_status_case(now: dt.datetime) -> Case`.

- [ ] **Step 1: write failing tests** — `tests/test_user_status.py`:

```python
"""Oś statusów widocznych dla użytkownika (issue #1).

Właściwość i wyrażenie SQL muszą się zgadzać na całej macierzy stanów —
to jedna para reguł, dwa wykonania.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from webapp.models import Meeting, USER_STATUS_ORDER, utcnow, user_status_case


def _m(**kw) -> Meeting:
    base = dict(
        id="bot-1",
        status_group="done",
        transcript_state="none",
        asset_state="none",
        recording_id="rec-1",
        media_expires_at=None,
    )
    base.update(kw)
    return Meeting(**base)


# (nazwa, pola nadpisujące _m(), oczekiwany status)
CASES = [
    ("transcript ready wygrywa ze wszystkim",
     dict(status_group="failed", transcript_state="ready", asset_state="failed"), "ready"),
    ("scheduled", dict(status_group="scheduled", recording_id=None), "upcoming"),
    ("joining", dict(status_group="joining", recording_id=None), "in_meeting"),
    ("recording", dict(status_group="recording", recording_id=None), "in_meeting"),
    ("bot failed", dict(status_group="failed", recording_id=None), "failed"),
    ("transcript failed mimo assets na dysku",
     dict(transcript_state="failed", asset_state="ready"), "failed"),
    ("asset failed", dict(asset_state="failed"), "failed"),
    ("asset fetching", dict(asset_state="fetching"), "processing"),
    ("transcript queued", dict(transcript_state="queued"), "processing"),
    ("transcript running z assets na dysku",
     dict(transcript_state="running", asset_state="ready"), "processing"),
    ("done + assets na dysku", dict(asset_state="ready"), "to_process"),
    ("expired + assets na dysku (dysk jest źródłem prawdy)",
     dict(status_group="expired", asset_state="ready"), "to_process"),
    ("done + recording, media bez TTL", dict(), "to_process"),
    ("done + recording, media żyje",
     dict(media_expires_at=utcnow() + dt.timedelta(hours=2)), "to_process"),
    ("done + recording, media wygasło",
     dict(media_expires_at=utcnow() - dt.timedelta(hours=2)), "no_recording"),
    ("done bez nagrania", dict(recording_id=None), "no_recording"),
    ("expired bez assetów", dict(status_group="expired", recording_id=None), "no_recording"),
]


@pytest.mark.parametrize("name,fields,expected", CASES, ids=[c[0] for c in CASES])
def test_user_status_property(name, fields, expected):
    assert _m(**fields).user_status == expected


def test_every_status_is_covered_by_cases():
    covered = {expected for _, _, expected in CASES}
    assert covered == set(USER_STATUS_ORDER)


def test_sql_case_agrees_with_property(session):
    """To samo wyrażenie filtro/fasetowe musi dawać identyczne wyniki."""
    for i, (_, fields, _) in enumerate(CASES):
        m = _m(**fields)
        m.id = f"bot-{i}"
        session.add(m)
    session.commit()
    rows = session.execute(
        select(Meeting.id, user_status_case(utcnow())).order_by(Meeting.id)
    ).all()
    by_id = {i: expected for i, (_, _, expected) in enumerate(CASES)}
    for mid, sql_status in rows:
        assert sql_status == by_id[int(mid.rsplit("-", 1)[1])]
```

- [ ] **Step 2: run, expect failure** — `uv run pytest tests/test_user_status.py -v` → ImportError (`USER_STATUS_ORDER` / `user_status_case` missing).
- [ ] **Step 3: implement** in `webapp/models.py` — extend the sqlalchemy import with `and_, case, or_`; after `status_group()` add:

```python
# Status widziany przez użytkownika — wyprowadzany, nie zapisywany.
# Jedna oś zamiast pary (status_group, transcript_state): kolejność to cykl
# życia (sidebar, sortowanie), etykiety mieszczą się w webapp/labels.py.
USER_STATUS_ORDER: tuple[str, ...] = (
    "upcoming", "in_meeting", "processing", "to_process",
    "ready", "failed", "no_recording",
)

# Widok domyślny „Finished”: wszystko, co się zakończyło — z transkryptem,
# do przetworzenia, nieudane i bez nagrania. Żywe stany (upcoming /
# in_meeting / processing) trzyma maszyna.
DEFAULT_VIEW_STATUSES: tuple[str, ...] = ("to_process", "ready", "failed", "no_recording")
```

…then on `Meeting`:

```python
    @property
    def user_status(self) -> str:
        """Status dla człowieka — patrz USER_STATUS_ORDER i issue #1.

        Kolejność ma znaczenie: gotowy transkrypt wygrywa nawet z padniętym
        botem, a wygasły media TTL z assetami na dysku to wciąż To process
        (dysk jest źródłem prawdy, nie Recall).
        """
        if self.transcript_state == "ready":
            return "ready"
        if self.status_group == "scheduled":
            return "upcoming"
        if self.status_group in ("joining", "recording"):
            return "in_meeting"
        if (
            self.status_group == "failed"
            or self.transcript_state == "failed"
            or self.asset_state == "failed"
        ):
            return "failed"
        if (
            self.asset_state in ("queued", "fetching")
            or self.transcript_state in ("queued", "running")
        ):
            return "processing"
        if (
            self.status_group in ("done", "expired")
            and self.transcript_state == "none"
            and (
                self.asset_state == "ready"
                or (
                    self.recording_id is not None
                    and (self.media_expires_at is None or self.media_expires_at > utcnow())
                )
            )
        ):
            return "to_process"
        return "no_recording"
```

and module-level:

```python
def user_status_case(now: dt.datetime) -> Case:
    """SQL-owy odpowiednik `Meeting.user_status` — WHERE / GROUP BY / ORDER BY.

    `now` jest parametrem, a nie `utcnow()` w środku, żeby jedno zapytanie
    (np. filtry + fasetty w jednym requeście) widziało jeden punkt w czasie.
    """
    media_live = or_(
        Meeting.media_expires_at.is_(None),
        Meeting.media_expires_at > now,
    )
    return case(
        (Meeting.transcript_state == "ready", "ready"),
        (Meeting.status_group == "scheduled", "upcoming"),
        (Meeting.status_group.in_(("joining", "recording")), "in_meeting"),
        (
            or_(
                Meeting.status_group == "failed",
                Meeting.transcript_state == "failed",
                Meeting.asset_state == "failed",
            ),
            "failed",
        ),
        (
            or_(
                Meeting.asset_state.in_(("queued", "fetching")),
                Meeting.transcript_state.in_(("queued", "running")),
            ),
            "processing",
        ),
        (
            and_(
                Meeting.status_group.in_(("done", "expired")),
                Meeting.transcript_state == "none",
                or_(
                    Meeting.asset_state == "ready",
                    and_(Meeting.recording_id.is_not(None), media_live),
                ),
            ),
            "to_process",
        ),
        else_="no_recording",
    )
```

- [ ] **Step 4: `uv run pytest tests/test_user_status.py -v` → all PASS.**
- [ ] **Step 5: commit** — `git commit -m "feat(webapp): derive user_status from internal meeting state"`

### Task 2: labels, badge macro, badge CSS

**Files:**
- Modify: `webapp/labels.py`, `webapp/templates/_ui.html` (`meeting_badge`), `webapp/static/app.css` (badge selectors), `webapp/app.py` (template globals — only the env lines, routes untouched here)
- Test: extend `tests/test_views.py`

**Interfaces:**
- Consumes: `Meeting.user_status` (Task 1).
- Produces: `labels.USER_STATUSES: dict[str, str]`, `labels.user_status(value) -> str`; template global `USER_STATUSES`; `_ui.meeting_badge(m)` renders `<span class="badge b-{{ m.user_status }}">Label</span>` with Recall tooltip; CSS classes `b-upcoming b-in_meeting b-processing b-to_process b-no_recording`.

- [ ] **Step 1: failing tests** in `tests/test_views.py`:

```python
def test_user_status_labels_cover_every_state():
    from webapp.models import USER_STATUS_ORDER
    for key in USER_STATUS_ORDER:
        assert labels.USER_STATUSES[key].strip()


def test_meeting_badge_shows_user_status_with_recall_tooltip(client, session, meeting):
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert 'class="badge b-to_process"' in r.text
    assert "To process" in r.text
    assert "Recall: done" in r.text
```

- [ ] **Step 2: run → FAIL** (labels.USER_STATUSES missing / badge still `b-done`).
- [ ] **Step 3: implement** — `labels.py` after TRANSCRIPT_STATES:

```python
# Status spotkania widoczny w UI — jedna oś wyprowadzana w webapp/models.py.
USER_STATUSES: dict[str, str] = {
    "upcoming": "Upcoming",
    "in_meeting": "In meeting",
    "processing": "Processing",
    "to_process": "To process",
    "ready": "Ready",
    "failed": "Failed",
    "no_recording": "No recording",
}


def user_status(value: Optional[str]) -> str:
    return _lookup(USER_STATUSES, value)
```

`app.py` globals: replace `templates.env.globals["STATUS_GROUPS"] = STATUS_GROUPS` with `templates.env.globals["USER_STATUSES"] = labels.USER_STATUSES`.

`_ui.html`:

```jinja
{% macro meeting_badge(m) -%}
  {%- set hint = status_hint(m.status_sub_code) -%}
  {{ badge('meeting', m.user_status, USER_STATUSES.get(m.user_status, m.user_status),
           'Recall: ' ~ (m.status_code or '?') ~ (' / ' ~ m.status_sub_code if m.status_sub_code else '')
           ~ (' — ' ~ hint if hint else '')) }}
{%- endmacro %}
```

`app.css` badge lines become (existing vars, no new colors):

```css
.b-done, .b-ready { background: var(--green-soft); color: var(--green); border-color: var(--green-line); }
.b-recording { background: var(--red-soft); color: var(--red); border-color: var(--red-line); }
.b-joining, .b-running, .b-in_meeting, .b-processing { background: var(--blue-soft); color: var(--blue); border-color: var(--blue-line); }
.b-scheduled, .b-queued, .b-none, .b-canceled, .b-upcoming, .b-no_recording { background: var(--bg-3); color: var(--ink-3); }
.b-failed { background: var(--red-soft); color: var(--red); border-color: var(--red-line); }
.b-expired, .b-fetching, .b-to_process { background: var(--amber-soft); color: var(--amber); border-color: var(--amber-line); }
```

- [ ] **Step 4: `uv run pytest tests/test_views.py -v` → PASS** (existing tests asserting `b-done` badges on the list/detail get updated in this task to `b-to_process` — `grep -rn 'b-done\|STATUS_GROUPS' tests/`).
- [ ] **Step 5: commit** — `git commit -m "feat(webapp): label and badge the derived user status"`

### Task 3: `queries.py` on the derived axis

**Files:**
- Modify: `webapp/queries.py`, `webapp/app.py` (`_filters` signature only, to compile)
- Test: `tests/test_queries.py`

**Interfaces:**
- Consumes: `user_status_case(now)`, `USER_STATUS_ORDER`, `DEFAULT_VIEW_STATUSES` (Task 1).
- Produces: `MeetingFilters(view: str = "", default_view: bool = False)` with `statuses` holding user-status keys and **no** `transcript` field; `apply_filters` filtering `user_status_case(utcnow()).in_(f.statuses)`; `status_facets` grouped by the case expression; `status_asc`/`status_desc` ranked by `USER_STATUS_ORDER` (via a rank built from the same case — no duplicated conditions); `TRANSCRIPT_FILTERS` deleted.

- [ ] **Step 1: failing tests** — update `tests/test_queries.py`: `_mk` gains `recording_id="rec-1"`, `asset_state="none"`, `media_expires_at=None`; the `seeded` fixture builds "Project Kickoff" with `recording_id=None` (done + no recording → `no_recording`); replace transcript-filter tests with:

```python
def test_filter_by_user_status(seeded):
    _, total = search_meetings(seeded, MeetingFilters(statuses=["failed"]))
    assert total == 1
    _, total = search_meetings(seeded, MeetingFilters(statuses=["no_recording"]))
    assert total == 1  # „Project Kickoff": done bez nagrania
    _, total = search_meetings(seeded, MeetingFilters(statuses=["ready", "failed", "no_recording"]))
    assert total == 3


def test_status_facets_count_derived_axis(seeded):
    facets = status_facets(seeded, MeetingFilters())
    assert facets.get("ready") == 1 and facets.get("failed") == 1 and facets.get("no_recording") == 1


def test_status_sort_ranks_lifecycle_first(seeded):
    rows, _ = search_meetings(seeded, MeetingFilters(sort="status_asc"))
    # ranga: ready(4) < failed(5) < no_recording(6); w tej samej randze OCCURRED desc
    assert [r.id for r in rows] == ["a" * 8, "c" * 8, "b" * 8]
    assert [r.user_status for r in rows] == ["ready", "failed", "no_recording"]
```

(Fixture dates: `a` = Aug 1 → `ready`; `b` = Aug 4, done without recording → `no_recording`; `c` = Jul 22, `failed`.)

- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement** in `queries.py`:
  - import `USER_STATUS_ORDER, user_status_case, utcnow` from models; drop `TRANSCRIPT_FILTERS` and `_TRANSCRIPT_RANK` (transcript *sort* keeps its rank expression — keep `_TRANSCRIPT_RANK`, only the filter axis dies);
  - rank helper (single source of truth — built from the case, not restated):

```python
def _user_status_rank(now: dt.datetime) -> Case:
    expr = user_status_case(now)
    return case(*[(expr == s, i) for i, s in enumerate(USER_STATUS_ORDER)], else_=9)
```

  - `MeetingFilters`: remove `transcript`; add `view: str = ""`, `default_view: bool = False`; `is_active()` → `bool(self.q or (self.statuses and not self.default_view) or self.participants or self.date_from or self.date_to)`; `as_query_dict()` → emit `status` only `if self.statuses and not self.default_view`, emit `view` only `if self.view == "all"`, drop the transcript line;
  - `apply_filters`: `if f.statuses: stmt = stmt.where(user_status_case(utcnow()).in_(f.statuses))` (delete the four `f.transcript` branches);
  - `_order`: `status_asc`/`status_desc` use `_user_status_rank(utcnow())`;
  - `status_facets`:

```python
def status_facets(session: Session, f: MeetingFilters) -> dict[str, int]:
    """Liczniki statusów przy pozostałych filtrach (bez filtra statusu)."""
    probe = MeetingFilters(**{**f.__dict__, "statuses": []})
    expr = user_status_case(utcnow())
    rows = session.execute(
        apply_filters(select(expr, func.count(Meeting.id)).group_by(expr), probe)
    ).all()
    return {str(k): int(v) for k, v in rows}
```

  - `app.py` `_filters`: drop the `transcript` parameter and its `MeetingFilters(...)` kwarg (route params change in Task 4; keep both endpoints compiling by removing the `transcript` argument from their calls too — `transcripts_view` currently passes `"ready"`).

- [ ] **Step 4: `uv run pytest tests/test_queries.py tests/test_user_status.py -v` → PASS.**
- [ ] **Step 5: commit** — `git commit -m "feat(webapp): filter, facet and sort meetings by derived status"`

### Task 4: routes — Finished default view, API field

**Files:**
- Modify: `webapp/app.py` (`_filters`, `meetings_view`, `api_meetings`, `transcripts_view`, `_meeting_json`, globals)
- Test: `tests/test_views.py`

**Interfaces:**
- Consumes: Task 3 filters.
- Produces: `GET /meetings` (and `/api/meetings`) accepting `view: str` (`"all"` = no seeding; anything else + no explicit `status` → seeded `DEFAULT_VIEW_STATUSES`, `f.default_view=True`); `_meeting_json` includes `"user_status"`; API defaults `view="all"` so integrations see everything; HTML default stays the Finished view; `/transcripts` CTA URL builds are template-side (Task 5).

- [ ] **Step 1: failing tests** in `tests/test_views.py`:

```python
def _seed_axis(session):
    when = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc)
    session.add(Meeting(id="bot-up", title="Planned", status_group="scheduled",
                        transcript_state="none", asset_state="none", started_at=when))
    session.add(Meeting(id="bot-none", title="Empty", status_group="done",
                        transcript_state="none", asset_state="none", started_at=when))
    session.add(Meeting(id="bot-ok", title="Done deal", status_group="done",
                        transcript_state="ready", asset_state="ready", started_at=when))
    session.commit()


def test_meetings_default_to_finished_view(client, session):
    _seed_axis(session)
    html = client.get("/meetings", headers=HTML).text
    assert "Empty" in html and "Done deal" in html
    assert "Planned" not in html
    assert "finished" in html.lower()          # podtytuł „N finished meetings”
    assert "Show all meetings" in html


def test_meetings_all_view_shows_everything(client, session):
    _seed_axis(session)
    html = client.get("/meetings?view=all", headers=HTML).text
    assert "Planned" in html and "Show finished only" in html


def test_status_deep_link_still_filters(client, session):
    _seed_axis(session)
    html = client.get("/meetings?status=ready", headers=HTML).text
    assert "Done deal" in html and "Empty" not in html


def test_api_exposes_user_status_and_defaults_to_all(client, session, meeting):
    data = client.get("/api/meetings").json()
    item = data["items"][0]
    assert item["user_status"] == "to_process"
    assert item["status"] == "done" and item["transcript_state"] == "none"
```

- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement** in `app.py`:

```python
def _filters(
    q: str = "",
    status: Optional[list[str]] = None,
    participant: Optional[list[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    view: str = "",
    sort: str = "date_desc",
    page: int = 1,
    per_page: int = 25,
) -> MeetingFilters:
    statuses = [s for s in (status or []) if s in USER_STATUS_ORDER]
    # Bez explicitego statusu pokazujemy widok „Finished” — wszystko, co się
    # zakończyło. `?view=all` lub dowolny status = pełna/jawna kontrola.
    default_view = not statuses and view != "all"
    return MeetingFilters(
        q=(q or "").strip(),
        statuses=statuses or (list(DEFAULT_VIEW_STATUSES) if default_view else []),
        participants=[p for p in (participant or []) if p],
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        view=view if view == "all" else "",
        default_view=default_view,
        sort=sort if sort in SORTS else "date_desc",
        page=max(1, page),
        per_page=min(100, max(5, per_page)),
    )
```

  - `meetings_view`: swap the `transcript: str = ""` query param for `view: str = ""`, pass it through; template context unchanged (templates read `f.default_view` / `f.view`).
  - `api_meetings`: same param swap, default `view: str = "all"`; `_meeting_json` gains `"user_status": m.user_status,` right after `"status"`.
  - `transcripts_view`: call `_filters(...)` without the removed `"ready"` arg.
  - imports: `USER_STATUS_ORDER`, `DEFAULT_VIEW_STATUSES` from models; drop `STATUS_GROUPS` / `TRANSCRIPT_FILTERS` imports + their `templates.env.globals` lines.
- [ ] **Step 4: `uv run pytest tests/test_views.py -v` → PASS** (fix any test that still hits `?transcript=` or asserts `STATUS_GROUPS`).
- [ ] **Step 5: commit** — `git commit -m "feat(webapp): default meetings view to Finished, expose user_status in API"`

### Task 5: templates — sidebar, chips, toggle, empty state, CTA

**Files:**
- Modify: `webapp/templates/_filters.html`, `webapp/templates/meetings.html`, `webapp/templates/transcripts.html`, `webapp/templates/meeting_detail.html` (one badge class), `webapp/static/app.css` (cache-bust `?v=` in `base.html`)
- Test: `tests/test_views.py`

**Interfaces:**
- Consumes: `USER_STATUSES` + `USER_STATUS_ORDER` globals, `f.default_view`, `f.view`, `facets` keyed by user status.
- Produces: sidebar with 7 status checkboxes (lifecycle order, counts); no transcript select; active chips labeled from `USER_STATUSES`; sub-header with view toggle; Finished-specific empty state; `/transcripts` CTA → `/meetings?status=to_process`; detail “No recording” chip uses `b-no_recording`.

- [ ] **Step 1: failing tests**:

```python
def test_sidebar_uses_single_status_axis(client, session, meeting):
    html = client.get("/meetings", headers=HTML).text
    for label in ("Upcoming", "In meeting", "Processing", "To process", "No recording"):
        assert f">{label}</span>" in html
    assert 'name="transcript"' not in html


def test_finished_empty_state_explains_itself(client, session):
    session.add(Meeting(id="bot-up", title="Planned", status_group="scheduled",
                        transcript_state="none", asset_state="none"))
    session.commit()
    html = client.get("/meetings", headers=HTML).text
    assert "No finished meetings yet" in html
    assert "Show all meetings" in html


def test_transcripts_empty_state_links_to_to_process(client, session):
    html = client.get("/transcripts", headers=HTML).text
    assert 'href="/meetings?status=to_process"' in html
```

- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement**:
  - `_filters.html`: status fieldset iterates `USER_STATUS_ORDER` with `USER_STATUSES[key]` labels and `facets.get(key, 0)` counts; delete the whole `show_transcript` block; after the hidden `sort` input add `{% if f.view == 'all' %}<input type="hidden" name="view" value="all">{% endif %}` (keeps the All view across form submits).
  - `meetings.html`:
    - sub-header: `{% if f.is_active() %}` unchanged wording; `{% elif f.default_view %}` → `{{ total }} finished {{ 'meeting' if total == 1 else 'meetings' }}. <a href="{{ qs(qbase, view='all', page=None) }}">Show all meetings</a>`; `{% elif f.view == 'all' %}` → plain count + `<a href="{{ qs(qbase, view=None, page=None) }}">Show finished only</a>`;
    - chips loop: `{{ USER_STATUSES[s] }}`; delete the transcript chip line;
    - empty states: insert between `f.is_active()` and the no-data branch: `ui.empty(ui.art_meetings(), 'No finished meetings yet', 'Meetings land here once they end — transcribed, waiting to process, or failed.')` with a `btn-primary` “Show all meetings” link (`qs(qbase, view='all', page=None)`).
  - `transcripts.html`: CTA `href="/meetings?status=to_process"`, label “Find a meeting to process”.
  - `meeting_detail.html`: `<span class="badge b-none">No recording</span>` → `b-no_recording`.
  - `base.html`: bump stylesheet `?v=` (e.g. `20260821-status`).
- [ ] **Step 4: `uv run pytest tests/test_views.py -v` → PASS.**
- [ ] **Step 5: commit** — `git commit -m "feat(webapp): render the single status axis in filters, chips and empty states"`

### Task 6: full verification + craft checklist

**Files:** none new.

- [ ] **Step 1:** `uv run pytest` → whole suite green (incl. `test_local_assets.py`, `test_jobs.py` which construct meetings with `status_group`).
- [ ] **Step 2:** Craft check (UI touched): states — loading N/A (server-rendered), empty/finished-empty/error(404) covered; no new imagery needed (existing `art_meetings` reused — deliberate no-art note); motion — none added; a11y — badge text is real text, `aria-sort` intact, checkboxes unchanged (≥44px targets, 360px sidebar unchanged from before); dark mode — badge colors come from theme vars.
- [ ] **Step 3:** `git status` clean, push `git push -u origin HEAD`.
- [ ] **Step 4:** Draft PR (dev-flow step 9) with the template body; design-decision section records the approved deviation (default view = “Finished”, includes Ready).
