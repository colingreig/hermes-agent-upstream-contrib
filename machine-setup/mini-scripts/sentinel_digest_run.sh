#!/bin/bash
# ignite-sentinel DAILY SWEEP + DIGEST launchd runner. Runs the once-daily
# retro-triage sweep, then sends the Lane-A summary email (see notify.py /
# monitor.py --sweep / --digest) via Postmark SMTP.
# Rebuilt 2026-07-22 after being lost in the 2026-07-19 home-directory wipe
# (ClickUp 86e29znk6; the plist survived in launchd's in-memory job table but
# this script and the plist file itself were both gone, so the job exited 127
# on every 07:00 tick — see ~/.hermes/logs/ignite-sentinel-digest-launchd.log).
#
# Resolves the sentinel runtime environment via the 1Password SDK resolver, same
# as the hourly monitor job. The sweep needs Sentry/ClickUp/LLM credentials; a
# Postmark-only env is not enough. Never shell out to the `op` CLI (hung under
# OP_SERVICE_ACCOUNT_TOKEN and took the gateway down 2026-07-04/05; see
# op_sdk_resolve.py's docstring).
#
# Composes SENTINEL_SMTP_URL HERE at runtime, NOT in op-secrets.env: its
# resolver only accepts bare `op://...` reference lines, and a composed
# `smtp://...` value with an embedded op:// reference silently fails to
# resolve. Postmark SMTP: host smtp.postmarkapp.com, port 587 STARTTLS,
# username == password == the Server API Token (see notify.py _send_smtp()
# and README.md "Email notifications").
set -uo pipefail

HOME_DIR="$HOME"
TOKEN_FILE="$HOME_DIR/.config/op-runtime-token"
ENV_FILE="$HOME_DIR/.hermes/scripts/op-secrets.env"
RESOLVER="$HOME_DIR/.hermes/scripts/op_sdk_resolve.py"
RESOLVER_PYTHON="$HOME_DIR/.hermes/runtime-current/venv/bin/python"
SENTINEL_DIR="$HOME_DIR/.hermes/repos/ignite-sentinel"
SENTINEL_PYTHON="$SENTINEL_DIR/venv/bin/python"
LOG="$HOME_DIR/.hermes/logs/ignite-sentinel-digest.log"
SWEEP_TIMEOUT_SECONDS="${SENTINEL_DIGEST_SWEEP_TIMEOUT_SECONDS:-1200}"

TS="$(date -u +%FT%TZ)"

if [ ! -x "$RESOLVER_PYTHON" ] || [ ! -f "$RESOLVER" ] || [ ! -f "$TOKEN_FILE" ] || [ ! -f "$ENV_FILE" ]; then
  echo "$TS sentinel_digest_run: FATAL: SDK resolver/venv/token/env file missing — refusing to run on unresolved secrets" >> "$LOG"
  exit 1
fi

resolved_env="$(mktemp)"
trap 'rm -f "$resolved_env"' EXIT

if ! "$RESOLVER_PYTHON" "$RESOLVER" "$ENV_FILE" > "$resolved_env" 2>>"$LOG"; then
  echo "$TS sentinel_digest_run: FATAL: 1Password SDK resolve failed — refusing to run" >> "$LOG"
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$resolved_env"
set +a

if [ -z "${POSTMARK_SERVER_TOKEN:-}" ]; then
  echo "$TS sentinel_digest_run: FATAL: POSTMARK_SERVER_TOKEN did not resolve — refusing to run" >> "$LOG"
  exit 1
fi

export SENTINEL_SMTP_URL="smtp://${POSTMARK_SERVER_TOKEN}:${POSTMARK_SERVER_TOKEN}@smtp.postmarkapp.com:587"

# Match sentinel_run.sh's diagnosis routing: use the local Hermes gateway
# instead of a raw provider key when the gateway key is readable. The disable
# knob is only for no-write smoke verification of this wrapper path.
if [ "${SENTINEL_DIGEST_DISABLE_LLM:-0}" = "1" ]; then
  unset SENTINEL_LLM_API_KEY ANTHROPIC_API_KEY
  echo "$TS sentinel_digest_run: LLM disabled by SENTINEL_DIGEST_DISABLE_LLM=1" >> "$LOG"
else
  GW_KEY="$("$RESOLVER_PYTHON" -c "
import yaml
d = yaml.safe_load(open('$HOME_DIR/.hermes/config.yaml'))
print(d.get('platforms', {}).get('api_server', {}).get('extra', {}).get('key', ''))
" 2>>"$LOG")"
  if [ -n "$GW_KEY" ]; then
    export SENTINEL_LLM_PROVIDER=openai
    export SENTINEL_LLM_BASE_URL="http://127.0.0.1:8642/v1"
    export SENTINEL_LLM_API_KEY="$GW_KEY"
    export SENTINEL_LLM_MODEL="glm"
  else
    echo "$TS sentinel_digest_run: WARNING: could not read local gateway key from config.yaml — sweep diagnosis will degrade to None" >> "$LOG"
  fi
fi

echo "$TS sentinel_digest_run: secrets resolved, running monitor.py --sweep then --digest" >> "$LOG"
cd "$SENTINEL_DIR" || exit 1
"$RESOLVER_PYTHON" - "$SWEEP_TIMEOUT_SECONDS" "$SENTINEL_PYTHON" -m ignite_sentinel.monitor --sweep --json >> "$LOG" 2>&1 <<'PY'
import subprocess
import sys

timeout = int(sys.argv[1])
cmd = sys.argv[2:]
try:
    raise SystemExit(subprocess.run(cmd, timeout=timeout).returncode)
except subprocess.TimeoutExpired:
    print(f"sentinel_digest_run: monitor.py --sweep timed out after {timeout}s", file=sys.stderr)
    raise SystemExit(124)
PY
sweep_status=$?
if [ "$sweep_status" -ne 0 ]; then
  echo "$TS sentinel_digest_run: FATAL: monitor.py --sweep exited $sweep_status; skipping digest" >> "$LOG"
  exit "$sweep_status"
fi
exec "$SENTINEL_PYTHON" -m ignite_sentinel.monitor --digest --json >> "$LOG" 2>&1
