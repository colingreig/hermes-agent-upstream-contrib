#!/bin/bash
# ignite-sentinel launchd runner. Resolves secrets via the 1Password
# service-account SDK (never the `op` CLI — op_sdk_resolve.py's docstring:
# `op read`/`op run` hung under OP_SERVICE_ACCOUNT_TOKEN and took the Hermes
# gateway down in a boot-crash loop on 2026-07-04/05). Same pattern as
# gateway_secrets_wrap.sh. Refuses to run on unresolved secrets rather than
# running dark against Sentry/ClickUp with a partial credential set.
set -uo pipefail

HOME_DIR="$HOME"
TOKEN_FILE="$HOME_DIR/.config/op-runtime-token"
ENV_FILE="$HOME_DIR/.hermes/scripts/op-secrets.env"
RESOLVER="$HOME_DIR/.hermes/scripts/op_sdk_resolve.py"
RESOLVER_PYTHON="$HOME_DIR/.hermes/runtime-current/venv/bin/python"
SENTINEL_DIR="$HOME_DIR/.hermes/repos/ignite-sentinel"
SENTINEL_PYTHON="$SENTINEL_DIR/venv/bin/python"
LOG="$HOME_DIR/.hermes/logs/ignite-sentinel.log"

# Launchd timers on several jobs can align after reboot. Spread this job's
# 1Password request across a bounded 0-120s window. The override is only for
# deterministic smoke tests and is clamped to the production maximum.
START_DELAY_MAX="${SENTINEL_START_DELAY_MAX_SECONDS:-120}"
case "$START_DELAY_MAX" in
  ''|*[!0-9]*) START_DELAY_MAX=120 ;;
esac
if (( START_DELAY_MAX > 120 )); then
  START_DELAY_MAX=120
fi
START_DELAY=$(( RANDOM % (START_DELAY_MAX + 1) ))
if (( START_DELAY > 0 )); then
  sleep "$START_DELAY"
fi

TS="$(date -u +%FT%TZ)"

if [ ! -x "$RESOLVER_PYTHON" ] || [ ! -f "$RESOLVER" ] || [ ! -f "$TOKEN_FILE" ] || [ ! -f "$ENV_FILE" ]; then
  echo "$TS sentinel_run: FATAL: SDK resolver/venv/token/env file missing — refusing to run on unresolved secrets" >> "$LOG"
  exit 1
fi

resolved_env="$(mktemp)"
trap 'rm -f "$resolved_env"' EXIT

if ! "$RESOLVER_PYTHON" "$RESOLVER" "$ENV_FILE" > "$resolved_env" 2>>"$LOG"; then
  echo "$TS sentinel_run: FATAL: 1Password SDK resolve failed — refusing to run" >> "$LOG"
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$resolved_env"
set +a

if [ "${SENTINEL_SMOKE_ONLY:-0}" = "1" ]; then
  echo "$TS sentinel_run: smoke-only secrets resolve passed (monitor/Slack suppressed)" >> "$LOG"
  exit 0
fi

# Diagnosis LLM: route through the local Hermes gateway's OpenAI-compatible
# api_server (127.0.0.1:8642) rather than a raw Anthropic key — keeps this
# inside Hermes's own provider routing/spend tracking instead of bypassing it.
# Anthropic was removed from the primary stack 2026-06-12; do not point this
# at api.anthropic.com. Read the gateway key fresh each run (not duplicated
# into a secrets store) since it already lives in config.yaml.
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
  echo "$TS sentinel_run: WARNING: could not read local gateway key from config.yaml — diagnosis will degrade to None (Lane A triage only, no self-heal this run)" >> "$LOG"
fi

echo "$TS sentinel_run: secrets resolved, running monitor.py" >> "$LOG"
cd "$SENTINEL_DIR" || exit 1
exec "$SENTINEL_PYTHON" -m ignite_sentinel.monitor --json >> "$LOG" 2>&1
