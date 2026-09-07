#!/usr/bin/env bash
# Save one HTTP exchange as evidence: status, headers, body.
#
#   scripts/capture.sh <name> <path-or-url> [curl args...]
#
# Writes $VERIFY_EVIDENCE/<name>.{status,headers,body}, appends the three
# paths to $VERIFY_EVIDENCE/manifest.txt and prints "<name> <status>".
# Relative paths are resolved against the run's VERIFY_URL. Redirects are NOT
# followed, so 303/302 targets stay visible in the headers file.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"
name="$1"; target="$2"; shift 2
# The name becomes a file stem inside the run's evidence dir; keep it there.
case "$name" in
  ""|.|..|*/*|*[!A-Za-z0-9._-]*) echo "capture: invalid name '$name' (use [A-Za-z0-9._-]+)" >&2; exit 1 ;;
esac
case "$target" in http://*|https://*) url="$target" ;; *) url="$VERIFY_URL$target" ;; esac
mkdir -p "$VERIFY_EVIDENCE"
out="$VERIFY_EVIDENCE/$name"
status="$(curl -s -m 30 -D "$out.headers" -o "$out.body" -w '%{http_code}' "$@" "$url")"
echo "$status" > "$out.status"
printf '%s\n%s\n%s\n' "$out.status" "$out.headers" "$out.body" >> "$VERIFY_EVIDENCE/manifest.txt"
echo "$name $status"
