#!/usr/bin/env bash
# Upload a built .ics to R2. NOT to be run until the operator is ready.
# Usage: FEEDS_BUCKET=<bucket> FEEDS_KEY=calendar/<jurisdiction>/<body>.ics [FEEDS_SLUG=provenage] bash publish-feed.sh [--dry-run] FILE.ics
set -euo pipefail

DRY=0
if [[ "${1:-}" == "--dry-run" ]]; then DRY=1; shift; fi

SRC="${1:?path to the .ics file to upload is required}"
FEEDS_SLUG="${FEEDS_SLUG:-provenage}"
KEY="${FEEDS_KEY:?FEEDS_KEY must be set, like calendar/<jurisdiction>/<body>.ics}"
[[ "$KEY" =~ ^calendar/[a-z0-9][a-z0-9._-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}\.ics$ ]] \
  || { echo "FEEDS_KEY must look like calendar/<jurisdiction>/<body>.ics (got: $KEY)" >&2; exit 1; }

: "${FEEDS_BUCKET:?FEEDS_BUCKET must be set (R2 bucket name)}"
[[ -f "$SRC" ]] || { echo "source not found: $SRC" >&2; exit 1; }

CMD=(npx wrangler r2 object put "${FEEDS_BUCKET}/${KEY}" --file "$SRC" --content-type "text/calendar; charset=utf-8" --remote)

if (( DRY )); then
  echo "[dry-run] ${CMD[*]}"
else
  "${CMD[@]}"
fi
echo "Public URL: https://api.1507.systems/${FEEDS_SLUG}/${KEY}"
echo "Webcal URL: webcal://api.1507.systems/${FEEDS_SLUG}/${KEY}"
