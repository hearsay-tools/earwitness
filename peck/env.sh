#!/usr/bin/env bash
# Source this file: `PECK_DATA=/tmp/peck-run-1 PECK_PORT=8811 source peck/env.sh`
# Everything the app stores lives under $PECK_DATA, so one directory = one run.
# Values set here win over a developer's .env (the app loads .env without
# override), and every paid or external integration is switched off.

: "${PECK_DATA:?set PECK_DATA to an empty directory for this run}"
: "${PECK_PORT:=8811}"
mkdir -p "$PECK_DATA"
PECK_DATA="$(cd "$PECK_DATA" && pwd)"
export PECK_DATA PECK_PORT

export DATABASE_URL="sqlite:///$PECK_DATA/webapp.db"
export RECALL_DIR="$PECK_DATA/recall"
export TRANSCRIPTS_DIR="$PECK_DATA/transcripts"
export BASE_URL="http://127.0.0.1:$PECK_PORT"
export SECRET_KEY="peck-not-a-secret"
export TZ=UTC

# Real sign-in guard on; people are signed in with a seeded session cookie.
export AUTH_DISABLED=
export ALLOWED_GOOGLE_DOMAINS=
export GOOGLE_CLIENT_ID=
export GOOGLE_CLIENT_SECRET=

# No external services, no background schedulers.
export RECALL_API_KEY=
export RECALL_GOOGLE_CLIENT_ID=
export ELEVENLABS_API_KEY=
export TRANSCRIPT_API_TOKEN=
export HONCHO_ENABLED=
export AUTOSYNC_INTERVAL=0
export AUTOPROCESS=
