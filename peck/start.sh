#!/usr/bin/env bash
# Start the web app in the foreground for one Peck run.
#   PECK_DATA=/tmp/peck-run-1 PECK_PORT=8811 peck/start.sh
# Ready when GET http://127.0.0.1:$PECK_PORT/healthz answers 200.
set -euo pipefail
cd "$(dirname "$0")/.."
source peck/env.sh
exec uv run uvicorn webapp.app:app --host 127.0.0.1 --port "$PECK_PORT"
