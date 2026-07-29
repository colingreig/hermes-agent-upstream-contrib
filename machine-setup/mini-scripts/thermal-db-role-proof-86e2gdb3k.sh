#!/bin/bash
set -euo pipefail
cd /Users/colingreig/dev/thermal
[ -n "${DATABASE_URL:-}" ]
[ -n "${THERMAL_APP_DATABASE_URL:-}" ]
[ "$DATABASE_URL" != "$THERMAL_APP_DATABASE_URL" ]
exec /opt/homebrew/opt/node/bin/node packages/db/scripts/check-production-roles.mjs
