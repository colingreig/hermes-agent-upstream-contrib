#!/bin/bash
# Persistent, visible Chrome CDP endpoint for the Hermes Mac mini.
#
# Chrome itself is loopback-only. Tailscale Serve adds a raw TCP forward on
# the mini's tailnet address so CDP is never exposed on a wildcard or LAN
# listener.
set -euo pipefail

CHROME_BIN="${CHROME_CDP_CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
TAILSCALE_BIN="${CHROME_CDP_TAILSCALE_BIN:-/Applications/Tailscale.app/Contents/MacOS/Tailscale}"
LSOF_BIN="${CHROME_CDP_LSOF_BIN:-/usr/sbin/lsof}"
HERMES_HOME="${HERMES_HOME:-/Users/colingreig/.hermes}"
PROFILE_DIR="$HERMES_HOME/chrome-cdp-profile"
CDP_PORT=9222
LOOPBACK_ENDPOINT="127.0.0.1:$CDP_PORT"

chrome_pid=""
serve_configured=0

log() {
  printf '%s chrome_cdp_launch: %s\n' "$(date -u +%FT%TZ)" "$*"
}

# Invoked indirectly by the EXIT trap below.
# shellcheck disable=SC2329
cleanup() {
  exit_status=$?
  trap - EXIT INT TERM HUP

  if [ "$serve_configured" -eq 1 ]; then
    if ! "$TAILSCALE_BIN" serve --yes --tcp="$CDP_PORT" off >/dev/null 2>&1; then
      [ "$exit_status" -ne 0 ] || exit_status=70
    fi
  fi

  if [ -n "$chrome_pid" ] && kill -0 "$chrome_pid" 2>/dev/null; then
    kill -TERM "$chrome_pid" 2>/dev/null || true
    wait "$chrome_pid" 2>/dev/null || true
  fi

  exit "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

for required in "$CHROME_BIN" "$TAILSCALE_BIN" "$LSOF_BIN"; do
  if [ ! -x "$required" ]; then
    log "required executable is missing: $required"
    exit 78
  fi
done

tailscale_ip="$("$TAILSCALE_BIN" ip -4)"
/usr/bin/python3 - "$tailscale_ip" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.ip_address(sys.argv[1])
except ValueError as exc:
    raise SystemExit(f"invalid Tailscale IPv4 address: {exc}")

if address not in ipaddress.ip_network("100.64.0.0/10"):
    raise SystemExit(f"refusing non-CGNAT Tailscale IPv4 address: {address}")
PY

if "$LSOF_BIN" -nP -iTCP:"$CDP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  log "port $CDP_PORT is already occupied; refusing to replace or share it"
  exit 78
fi

/bin/mkdir -p "$PROFILE_DIR" "$HERMES_HOME/logs"
/bin/chmod 700 "$PROFILE_DIR"

"$CHROME_BIN" \
  --user-data-dir="$PROFILE_DIR" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port="$CDP_PORT" \
  --no-first-run \
  --no-default-browser-check \
  --new-window \
  about:blank &
chrome_pid=$!

listener_endpoints=""
for _attempt in $(/usr/bin/seq 1 60); do
  if ! kill -0 "$chrome_pid" 2>/dev/null; then
    wait "$chrome_pid" || true
    log "Chrome exited before CDP became ready"
    exit 70
  fi

  listener_endpoints="$(
    "$LSOF_BIN" -nP -iTCP:"$CDP_PORT" -sTCP:LISTEN 2>/dev/null |
      /usr/bin/awk 'NR > 1 {print $9}' || true
  )"
  if [ -n "$listener_endpoints" ]; then
    break
  fi
  /bin/sleep 0.25
done

if [ -z "$listener_endpoints" ]; then
  log "Chrome did not open CDP port $CDP_PORT within 15 seconds"
  exit 70
fi

while IFS= read -r endpoint; do
  case "$endpoint" in
    "$LOOPBACK_ENDPOINT"|"[::1]:$CDP_PORT") ;;
    *)
      log "unsafe CDP listener detected at $endpoint; refusing Tailscale forwarding"
      exit 78
      ;;
  esac
done <<<"$listener_endpoints"

"$TAILSCALE_BIN" serve \
  --bg \
  --yes \
  --tcp="$CDP_PORT" \
  "tcp://$LOOPBACK_ENDPOINT" >/dev/null
serve_configured=1

serve_status="$("$TAILSCALE_BIN" serve status --json)"
printf '%s' "$serve_status" | /usr/bin/python3 -c '
import json
import sys

port = sys.argv[1]
expected = f"127.0.0.1:{port}"
status = json.load(sys.stdin)
entry = status.get("TCP", {}).get(port, {})
if entry.get("TCPForward") != expected:
    raise SystemExit(
        f"refusing unverified Tailscale Serve target: {entry!r}; expected {expected}"
    )
' "$CDP_PORT"

log "ready: Chrome pid=$chrome_pid loopback=$LOOPBACK_ENDPOINT tailnet=$tailscale_ip:$CDP_PORT"

set +e
wait "$chrome_pid"
chrome_status=$?
set -e
chrome_pid=""
exit "$chrome_status"
