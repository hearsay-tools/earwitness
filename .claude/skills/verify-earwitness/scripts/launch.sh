#!/usr/bin/env bash
# Start an isolated Earwitness webapp (and optionally a worker) for one run.
#
#   scripts/launch.sh [--worker] [run-id]
#
# Prints the run id and URL. Re-run with the same run id to reuse its state.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WITH_WORKER=0
for a in "$@"; do
  case "$a" in
    --worker) WITH_WORKER=1 ;;
    *) VERIFY_RUN_ID="$a" ;;
  esac
done
export VERIFY_RUN_ID="${VERIFY_RUN_ID:-}"
[ -n "$VERIFY_RUN_ID" ] || unset VERIFY_RUN_ID
# shellcheck source=env.sh
source "$HERE/env.sh"

mkdir -p "$VERIFY_STATE" "$VERIFY_EVIDENCE" "$RECALL_DIR" "$TRANSCRIPTS_DIR"
cd "$VERIFY_REPO_ROOT"

if [ -f "$VERIFY_STATE/web.pid" ] && kill -0 "$(cat "$VERIFY_STATE/web.pid")" 2>/dev/null; then
  # Reusing a live run: the markers describe THAT server, so they are not
  # rewritten. A request that contradicts them is a different run.
  if [ -f "$VERIFY_STATE/auth" ] && [ "$(cat "$VERIFY_STATE/auth")" != "$VERIFY_AUTH" ]; then
    echo "run $VERIFY_RUN_ID already serves auth=$(cat "$VERIFY_STATE/auth"); asked for $VERIFY_AUTH." >&2
    echo "cleanup.sh $VERIFY_RUN_ID first, or use another run id." >&2
    exit 1
  fi
  if [ -f "$VERIFY_STATE/commit" ] && [ "$(cat "$VERIFY_STATE/commit")" != "$(git rev-parse HEAD)" ]; then
    echo "warn: web already running (pid $(cat "$VERIFY_STATE/web.pid")) from $(cut -c1-7 "$VERIFY_STATE/commit"); HEAD is now $(git rev-parse --short HEAD). cleanup.sh + launch.sh to pick up the change." >&2
  else
    echo "web already running (pid $(cat "$VERIFY_STATE/web.pid"))"
  fi
else
  echo "$VERIFY_PORT" > "$VERIFY_STATE/port"
  echo "$VERIFY_AUTH" > "$VERIFY_STATE/auth"
  git rev-parse HEAD > "$VERIFY_STATE/commit"
  nohup uv run uvicorn webapp.app:app --host 127.0.0.1 --port "$VERIFY_PORT" \
    > "$VERIFY_STATE/web.log" 2>&1 &
  echo $! > "$VERIFY_STATE/web.pid"
fi

if [ "$WITH_WORKER" = 1 ]; then
  if [ -f "$VERIFY_STATE/worker.pid" ] && kill -0 "$(cat "$VERIFY_STATE/worker.pid")" 2>/dev/null; then
    echo "worker already running (pid $(cat "$VERIFY_STATE/worker.pid"))"
  else
    nohup uv run python -m webapp.worker -c 1 > "$VERIFY_STATE/worker.log" 2>&1 &
    echo $! > "$VERIFY_STATE/worker.pid"
  fi
fi

# Readiness: /healthz answers {"ok": true, ...} once the DB schema exists.
for _ in $(seq 1 60); do
  if curl -fsS "$VERIFY_URL/healthz" 2>/dev/null | grep -q '"ok":true'; then
    echo "run:   $VERIFY_RUN_ID"
    echo "url:   $VERIFY_URL"
    echo "state: $VERIFY_STATE"
    echo "evidence: $VERIFY_EVIDENCE"
    exit 0
  fi
  sleep 0.5
done
echo "webapp did not become ready within 30s; last log lines:" >&2
tail -n 30 "$VERIFY_STATE/web.log" >&2
exit 1
