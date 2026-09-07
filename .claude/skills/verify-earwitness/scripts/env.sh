#!/usr/bin/env bash
# Source this file. It exports the isolated environment one verification run
# uses for the Earwitness webapp: own SQLite file, own asset/transcript dirs,
# own port, auth disabled, scheduler off.
#
#   source .claude/skills/verify-earwitness/scripts/env.sh [run-id]
#
# The run id defaults to VERIFY_RUN_ID, then $1, then a fresh timestamp.
# `webapp/config.py` calls load_dotenv() WITHOUT override, so everything
# exported here wins over a `.env` in the repo root. API keys from `.env`
# (RECALL_API_KEY, ELEVENLABS_API_KEY, GOOGLE_*) still flow through when
# present; this file never sets or clears them.

VERIFY_REPO_ROOT="$(git rev-parse --show-toplevel)"
export VERIFY_REPO_ROOT
export VERIFY_RUN_ID="${VERIFY_RUN_ID:-${1:-$(date +%Y%m%d-%H%M%S)-$$}}"
# The id becomes a path segment under output/verify and cleanup runs
# `rm -rf` on it, so it must be one plain segment: no slashes, no dot-dirs.
case "$VERIFY_RUN_ID" in
  ""|.|..|*/*|*[!A-Za-z0-9._-]*)
    echo "verify-earwitness: invalid run id '$VERIFY_RUN_ID' (use [A-Za-z0-9._-]+)" >&2
    return 1 2>/dev/null || exit 1 ;;
esac
export VERIFY_STATE="$VERIFY_REPO_ROOT/output/verify/$VERIFY_RUN_ID"
export VERIFY_EVIDENCE="$VERIFY_REPO_ROOT/.verification-evidence/$VERIFY_RUN_ID"

# One port per run. Persisted in the state dir so every script of the run
# talks to the same instance.
if [ -f "$VERIFY_STATE/port" ]; then
  VERIFY_PORT="$(cat "$VERIFY_STATE/port")"
else
  VERIFY_PORT="${VERIFY_PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')}"
fi
export VERIFY_PORT
export VERIFY_URL="http://127.0.0.1:$VERIFY_PORT"

export DATABASE_URL="sqlite:///$VERIFY_STATE/webapp.db"
export RECALL_DIR="$VERIFY_STATE/recall"
export TRANSCRIPTS_DIR="$VERIFY_STATE/transcripts"
export BASE_URL="$VERIFY_URL"
export SECRET_KEY="verify-run-not-a-secret"
# VERIFY_AUTH=google keeps the real auth guard on (needs GOOGLE_CLIENT_ID/SECRET
# for the login itself; the redirect-to-login behaviour needs nothing).
# launch.sh persists the mode in the state dir so doctor/capture in another
# shell see the mode the server actually runs with, not this shell's default.
if [ -z "${VERIFY_AUTH:-}" ] && [ -f "$VERIFY_STATE/auth" ]; then
  VERIFY_AUTH="$(cat "$VERIFY_STATE/auth")"
fi
export VERIFY_AUTH="${VERIFY_AUTH:-disabled}"
if [ "$VERIFY_AUTH" = "disabled" ]; then
  export AUTH_DISABLED=1
else
  export AUTH_DISABLED=
fi
export AUTOSYNC_INTERVAL=0
export AUTOPROCESS=
export WORKER_CONCURRENCY=1
export WORKER_POLL_INTERVAL=0.5
