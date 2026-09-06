# Recall Calendar V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Ship authenticated Calendar V1 management with four recording choices and stable account ownership.

**Architecture:** A synchronous httpx Calendar client owns the upstream contract; SQLAlchemy tables own stable identities and OAuth attempts. A separate FastAPI router renders the existing Jinja2 design system and delegates scheduling to Recall.

**Tech Stack:** Python 3.12, uv, FastAPI, SQLAlchemy, httpx, Jinja2; no new production dependencies.

**Spec:** `docs/superpowers/specs/2026-09-06-recall-calendar-v1-design.md`

## Global Constraints

- Keep Google app login and its enrichment credentials separate from Recall authorization.
- Browser inputs never select a Recall identity.
- Disconnect only the Google platform, leaving the app session, Google enrichment tokens and recording history intact.
- Four choices: Off, External only, Internal only, All eligible meetings; preserve Custom and unrelated preferences.
- Reuse Apptension macros, labels and CSS tokens; 360×640, both themes, reduced motion, ≥44px targets.
- Never commit credentials, personal event data, recordings or bot IDs.
- Verify with `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`.

## Task 1: Calendar API and stable identity

**Files:** create `webapp/recall_calendar.py`, `webapp/calendar_identity.py`, `tests/test_recall_calendar.py`; modify `webapp/models.py`.

**Interfaces:** `CalendarClient(external_id: str)` context manager exposes `authenticate() -> str`, `get_user() -> dict`, `set_mode(mode: str) -> dict`, `disconnect() -> dict`, `meetings() -> list[dict]`. `CalendarError` contains safe human-facing copy. `recording_mode(preferences: dict) -> str` returns off/external/internal/all/custom. `ensure_identity(session, user) -> CalendarIdentity` persists a unique external_id before API auth. `find_identity(session, user)` performs no creation.

- [x] Write tests using httpx MockTransport against the real client: literal six-flag payloads, Custom detection, preservation of bot_name, same-identity token refresh, timeout/rate-limit safe errors, list and paginated meeting responses, rejection of foreign pagination URLs. Example observable assertion:
  ```python
  assert recording_mode({'record_external': True, 'record_internal': False,
                         'record_non_host': False, 'record_recurring': False,
                         'record_confirmed': False, 'record_only_host': False}) == 'external'
  ```
- [x] Add identity reuse, distinct users, unique external identity, and existing-database initialization tests; run `uv run pytest tests/test_recall_calendar.py -q` and observe missing behavior.
- [x] Implement client using regional `RecallConfig`; workspace Token header only for authenticate/list users, per-user header elsewhere. PUT only documented preference flags; read back preferences. HTTP failures never expose response text. Meetings use start_time_after/before and same-origin/path pagination with a bounded page count.
- [x] Add CalendarIdentity table keyed by User.id with unique external_id; insert in a nested transaction, resolve concurrent unique conflict to persisted winner. Refuse automatic new identities if workspace email lookup finds an existing Google connection; require operator adoption.
- [x] Run focused tests and ruff, inspect diff, commit task.

## Task 2: OAuth, management UI and ownership adoption

**Files:** create `webapp/calendar_routes.py`, `webapp/calendar_admin.py`, `webapp/templates/calendar.html`, `tests/test_calendar_routes.py`, `tests/test_calendar_admin.py`; modify `webapp/models.py`, `webapp/config.py`, `webapp/app.py`, `webapp/labels.py`, `webapp/templates/base.html`, `webapp/static/app.css`.

**Consumes:** Task 1 interfaces. **Produces:** `/calendar`, POST `/calendar/connect`, `/calendar/preferences`, `/calendar/disconnect`, GET `/calendar/oauth/callback`, `/calendar/oauth/return`, and `uv run python -m webapp.calendar_admin adopt --user-email EMAIL --external-id ID`.

- [x] Add route tests through TestClient with real DB and mocked upstream transport. Unconfigured page renders setup recovery; anonymous management cannot proceed. Extract CSRF token from returned form, submit mode, inspect authorization URL, simulate bridge and return. Assert forged state, wrong session, expired/replayed attempts never redirect to Recall. Test selected mode survives failure, preferences persist on reload, Custom preserved on reconnect, disconnect preserves local user/history.
- [x] Run `uv run pytest tests/test_calendar_routes.py -q` to observe missing routes.
- [x] Add expiring CalendarOAuthAttempt table storing hashed state and session binding, return nonce, bridge/return consumption, and requested mode. Use conditional UPDATE for consumption and invalidate older attempts on new initiation/disconnect. Use configured BASE_URL and regional callback only. Exact OAuth state goes to Google; no workspace key does. Final return re-reads connection, then verifies requested preferences for new setup.
- [x] Implement CSRF-protected forms and safe error redirects. Configuration adds RECALL_GOOGLE_CLIENT_ID. New identities require explicit mode; existing connected users keep preferences on reconnect. Do not initialize identities on GET. OAuth routes still require app session. Add callback log redaction and no-store/no-referrer responses.
- [x] Render connection, four-choice fieldset, and upcoming event list using existing macros/tokens. Show bot status from bot_id; explain overrides, domain comparison, syncing and eligible meetings. Preserve shell during fetch/form actions with aria-busy feedback and reduced motion. Add navigation link and first-login redirect to calendar page, preserving existing login intent where applicable.
- [x] Implement operator adoption via workspace user lookup before any token minting; verify exact external_id and Google email against active local user, reject conflicts or already-issued new identity. Test mismatch and repeated adoption. No browser adoption endpoint.
- [x] Run focused route/admin tests and ruff, inspect diff, commit task.

## Task 3: Integration, deployment and verification

**Files:** create `docs/recall-calendar.md`; modify README and applicable env example; amend implementation/tests only for verified defects.

- [x] Document regional workspace Google OAuth credentials, app callback and scope configuration, consent testing restrictions, stable identity adoption, controlled fallback, log redaction and four modes.
- [x] Run all three bound checks; fix failures based on evidence.
- [x] Launch local app with synthetic Recall fixtures; inspect desktop and 360×640 light/night layouts, keyboard focus, hit targets, reduced motion and loading/error states. Record actual observations.
- [ ] Run permitted live account/meeting acceptance if credentials and browser session are available; otherwise explicitly record the missing evidence and leave the issue open. Do not claim mocked scheduling demonstrates an actual bot joining.
- [x] Request code review of the complete diff; address correctness/security findings and repeat covering checks.
- [ ] Commit, push branch, open draft PR with repository template and required dev-flow fields, move board to In review, monitor CI/review using pr-checks. Keep draft and report any live acceptance blocker.
