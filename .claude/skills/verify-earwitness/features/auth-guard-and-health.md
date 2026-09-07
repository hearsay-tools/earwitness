# Auth guard and health endpoint

Source: `webapp/app.py:auth_guard` middleware and `PUBLIC_PATHS`, `healthz`,
`login_page` (`GET /login`), `auth_google`, `auth_callback`, `logout`;
`webapp/auth.py` (`get_current_user`, `dev_user`, `is_domain_allowed`);
`webapp/config.py` (`AUTH_DISABLED`, `ALLOWED_GOOGLE_DOMAINS`); template `login.html`.

## Outcome

Anonymous visitors are sent to the login page, API calls get 401 JSON,
`/healthz` and static files stay public, and with `AUTH_DISABLED=1` a
local developer is signed in as `dev@localhost`.

## Reach it

Two runs: one with auth disabled (default), one with `VERIFY_AUTH=google`.
The Google round trip itself needs `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`
(unresolved prerequisite here); the guard does not.

## Recipe

```bash
# run A: auth disabled (default launch)
$SKILL/scripts/capture.sh health /healthz
$SKILL/scripts/capture.sh dev-user /meetings -H 'accept: text/html'
# run B: guard on
VERIFY_AUTH=google $SKILL/scripts/launch.sh authrun
( source $SKILL/scripts/env.sh authrun
  $SKILL/scripts/capture.sh guard-html /meetings -H 'accept: text/html'
  $SKILL/scripts/capture.sh guard-api /api/meetings
  $SKILL/scripts/capture.sh guard-login /login -H 'accept: text/html'
  $SKILL/scripts/capture.sh guard-health /healthz
  $SKILL/scripts/capture.sh guard-static /static/app.css )
$SKILL/scripts/cleanup.sh authrun
```

## Success criteria

- `health.body` is `{"ok":true,"queue":{"queued":N,"running":N,"failed":N}}`, no auth required.
- `dev-user.body` contains `dev@localhost` or `Dev (auth disabled)` in the nav.
- `guard-html.status` 302 with `location: /login?next=/meetings`; `guard-api.status` 401 with `{"detail":"Sign-in required"}`.
- `guard-login.status` 200 and the page renders (it says login is not configured when OAuth env is missing).
- `guard-health.status` 200, `guard-static.status` 200.

## Gotchas

- Session cookie is `https_only` when `BASE_URL` starts with https; env.sh uses http.
- `ALLOWED_GOOGLE_DOMAINS` is enforced in `upsert_user` after the callback; not provable offline.

Evidence to save: all captures, from both runs.
