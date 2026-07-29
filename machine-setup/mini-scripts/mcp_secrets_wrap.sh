#!/bin/bash
# Secrets wrap for the `hermes` MCP server registered in Claude Code's
# ~/.claude.json (mcpServers.hermes): 1Password-only, via the official
# service-account SDK (never the `op` CLI — see op_sdk_resolve.py's module
# docstring: `op read`/`op run` hang under OP_SERVICE_ACCOUNT_TOKEN and, when the
# desktop app is running, any op call that misses the token pops a per-subprocess
# OS consent dialog in a loop). Migrated off `op read`/`op run` 2026-07-05 to
# match gateway_secrets_wrap.sh (op-cli-dialog-loop incident).
#
# Doppler was decommissioned 2026-07-03 and is NOT a fallback here — booting
# with unresolved 1Password secrets is worse than not booting. If secret
# resolution fails after retries, this wrapper refuses to launch the service
# and exits non-zero.
set -uo pipefail

HOME_DIR="$HOME"
TOKEN_FILE="$HOME_DIR/.config/op-runtime-token"
ENV_FILE="$HOME_DIR/.hermes/scripts/op-secrets.env"
RESOLVER="$HOME_DIR/.hermes/scripts/op_sdk_resolve.py"
RESOLVER_PYTHON="$HOME_DIR/.hermes/hermes-agent/venv/bin/python"
LOG="$HOME_DIR/.hermes/logs/mcp-secrets-wrap.log"

# run a command with a hard timeout (portable; no timeout(1)/gtimeout on this box)
# usage: _run_with_timeout <seconds> <cmd...>; returns 124 on timeout
_run_with_timeout() {
  local secs="$1"; shift
  "$@" & local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1; waited=$((waited+1))
    if [ "$waited" -ge "$secs" ]; then kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return 124; fi
  done
  wait "$pid" 2>/dev/null
}

TS="$(date -u +%FT%TZ)"

resolved=false
resolved_env="$(mktemp)"
trap 'rm -f "$resolved_env"' EXIT

if [ -x "$RESOLVER_PYTHON" ] && [ -f "$RESOLVER" ] && [ -f "$TOKEN_FILE" ] && [ -f "$ENV_FILE" ]; then
  attempt=1
  while [ "$attempt" -le 3 ]; do
    ts_attempt="$(date -u +%FT%TZ)"
    resolver_err="$(mktemp)"
    if _run_with_timeout 30 "$RESOLVER_PYTHON" "$RESOLVER" "$ENV_FILE" >"$resolved_env" 2>"$resolver_err"; then
      echo "$ts_attempt mcp_secrets_wrap: 1Password SDK resolve succeeded (attempt $attempt/3) — $(tail -1 "$resolver_err")" >> "$LOG"
      resolved=true
      rm -f "$resolver_err"
      break
    else
      rc=$?
      if [ "$rc" -eq 124 ]; then
        echo "$ts_attempt mcp_secrets_wrap: 1Password SDK resolve TIMED OUT after 30s (attempt $attempt/3)" >> "$LOG"
      else
        echo "$ts_attempt mcp_secrets_wrap: 1Password SDK resolve failed rc=$rc (attempt $attempt/3): $(cat "$resolver_err")" >> "$LOG"
      fi
      rm -f "$resolver_err"
    fi
    attempt=$((attempt+1))
    if [ "$attempt" -le 3 ]; then sleep 2; fi
  done

  if [ "$resolved" != true ]; then
    echo "$(date -u +%FT%TZ) mcp_secrets_wrap: >>> FATAL: 1Password unreachable via SDK after 3 attempts over ~90s — refusing to boot on unresolved secrets; launchd will relaunch" >> "$LOG"
  fi
else
  echo "$TS mcp_secrets_wrap: >>> FATAL: SDK resolver/venv/token/env file missing — refusing to boot on unresolved secrets; launchd will relaunch" >> "$LOG"
fi

if [ "$resolved" = true ]; then
  echo "$TS mcp_secrets_wrap: using 1Password service-account SDK" >> "$LOG"
  set -a
  # shellcheck disable=SC1090
  . "$resolved_env"
  set +a
  exec hermes mcp serve
fi

exit 1
