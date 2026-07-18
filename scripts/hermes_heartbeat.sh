#!/bin/bash
# hermes_heartbeat.sh — dead-man heartbeat for the Hermes mini (ClickUp 86e2bjabd).
#
# Curls an external healthchecks.io check every run (launchd StartInterval=600 =
# every 10 min). If the mini dies (power loss, hang, network), the pings STOP and
# healthchecks.io — configured with a 10-min period + 30-min grace — emails
# colin@ignitemarketing.com. This is the EXTERNAL half of availability hardening:
# the mini cannot alert about its own death, so an off-box watcher must.
#
# The ping URL is intentionally NOT hardcoded. It is read from (first hit wins):
#   1. $HERMES_HEARTBEAT_URL (env)
#   2. ~/.hermes/heartbeat-url.txt  (single line: the https://hc-ping.com/<uuid> URL)
# If neither is set the script is DORMANT: it logs once and exits 0 (no error, no
# spam) so it can be installed before the healthchecks.io check exists, then
# self-activates the moment the URL file is written. See the activation task.
set -u
LOG="$HOME/.hermes/logs/heartbeat.log"
mkdir -p "$(dirname "$LOG")"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

URL="${HERMES_HEARTBEAT_URL:-}"
if [ -z "$URL" ] && [ -f "$HOME/.hermes/heartbeat-url.txt" ]; then
  URL="$(tr -d '[:space:]' < "$HOME/.hermes/heartbeat-url.txt")"
fi

if [ -z "$URL" ]; then
  echo "$(ts) DORMANT: no heartbeat URL configured (set ~/.hermes/heartbeat-url.txt); skipping ping." >> "$LOG"
  exit 0
fi

# Ping with a short timeout; healthchecks.io treats any 2xx as alive.
if curl -fsS -m 20 --retry 2 "$URL" >/dev/null 2>&1; then
  echo "$(ts) OK: heartbeat ping delivered." >> "$LOG"
  exit 0
else
  rc=$?
  echo "$(ts) WARN: heartbeat ping FAILED (curl rc=$rc). Will retry next interval." >> "$LOG"
  # Non-zero so launchd records the failure, but the dead-man is the real signal.
  exit "$rc"
fi
