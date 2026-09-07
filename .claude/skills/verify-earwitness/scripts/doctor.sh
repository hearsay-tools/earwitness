#!/usr/bin/env bash
# Read-only health check of one run's instance. Never mutates state.
#
#   scripts/doctor.sh [run-id]
#
# Exit 0 = ready to drive. Exit 1 = something listed below failed.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
[ $# -ge 1 ] && export VERIFY_RUN_ID="$1"
# shellcheck source=env.sh
source "$HERE/env.sh"
fail=0
ok()   { echo "ok    $*"; }
bad()  { echo "FAIL  $*"; fail=1; }
warn() { echo "warn  $*"; }

[ -d "$VERIFY_STATE" ] && ok "state dir $VERIFY_STATE" || bad "no state dir for run $VERIFY_RUN_ID (launch first)"

if [ -f "$VERIFY_STATE/web.pid" ] && kill -0 "$(cat "$VERIFY_STATE/web.pid")" 2>/dev/null; then
  ok "web process alive (pid $(cat "$VERIFY_STATE/web.pid"))"
else
  bad "web process not running"
fi

if [ -f "$VERIFY_STATE/worker.pid" ]; then
  if kill -0 "$(cat "$VERIFY_STATE/worker.pid")" 2>/dev/null; then
    ok "worker process alive (pid $(cat "$VERIFY_STATE/worker.pid"))"
  else
    bad "worker pid file present but process dead"
  fi
else
  warn "no worker started for this run (jobs stay queued; use --worker or worker --once)"
fi

health="$(curl -fsS -m 5 "$VERIFY_URL/healthz" 2>/dev/null)"
if echo "$health" | grep -q '"ok":true'; then
  ok "GET /healthz -> $health"
else
  bad "GET /healthz on $VERIFY_URL failed: '${health:-no response}'"
fi

# The instance answering must be ours: uvicorn's cmdline carries the port.
if [ -f "$VERIFY_STATE/web.pid" ]; then
  cmd="$(tr '\0' ' ' < "/proc/$(cat "$VERIFY_STATE/web.pid")/cmdline" 2>/dev/null || true)"
  case "$cmd" in
    *"--port $VERIFY_PORT"*) ok "pid owns port $VERIFY_PORT" ;;
    *) bad "pid $(cat "$VERIFY_STATE/web.pid") is not uvicorn on port $VERIFY_PORT: '$cmd'" ;;
  esac
fi

if [ -f "$VERIFY_STATE/commit" ]; then
  launched="$(cat "$VERIFY_STATE/commit")"; now="$(git -C "$VERIFY_REPO_ROOT" rev-parse HEAD)"
  [ "$launched" = "$now" ] && ok "build: HEAD $now unchanged since launch" \
    || warn "HEAD moved since launch ($launched -> $now); uvicorn has no --reload here, restart the run"
fi

code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -H 'accept: text/html' "$VERIFY_URL/meetings")"
if [ -n "${AUTH_DISABLED:-}" ]; then
  [ "$code" = "200" ] && ok "auth disabled: GET /meetings -> 200 as dev@localhost" || bad "GET /meetings -> $code (expected 200 with AUTH_DISABLED=1)"
else
  [ "$code" = "302" ] && ok "auth guard on: GET /meetings -> 302 to /login" || bad "GET /meetings -> $code (expected 302 with auth on)"
fi

for k in RECALL_API_KEY ELEVENLABS_API_KEY GOOGLE_CLIENT_ID; do
  v="$(cd "$VERIFY_REPO_ROOT" && uv run python -c "import os;from dotenv import load_dotenv;load_dotenv();print(bool(os.environ.get('$k')))" 2>/dev/null)"
  [ "$v" = "True" ] && ok "$k present (integration reachable)" || warn "$k missing: features needing it are unverifiable in this run"
done

exit $fail
