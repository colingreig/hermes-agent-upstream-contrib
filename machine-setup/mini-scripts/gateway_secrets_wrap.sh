#!/bin/bash
# Canonical launchd boundary for the Hermes gateway.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
RUNTIME_PYTHON="$HERMES_HOME/runtime-current/venv/bin/python"
RESOLVER="$HERMES_HOME/scripts/op_sdk_resolve.py"
REFERENCE_FILE="$HERMES_HOME/scripts/launchd-secrets.op-env"
TOKEN_MINTER="$HERMES_HOME/scripts/github_app_token.py"
INNER="$HERMES_HOME/scripts/gateway_launch_inner.sh"
LOG="$HERMES_HOME/logs/gateway.error.log"
EXIT_TRANSIENT=75
EXIT_AUTH=77

mkdir -p "$(dirname "$LOG")"
timestamp() { date -u +%FT%TZ; }
fatal() {
  printf '%s gateway_secrets_wrap: FATAL classification=%s exit=%s: %s\n' \
    "$(timestamp)" "$1" "$2" "$3" >>"$LOG"
}

for required in "$RUNTIME_PYTHON" "$RESOLVER" "$REFERENCE_FILE" "$TOKEN_MINTER" "$INNER"; do
  if [ ! -f "$required" ] || { [ "$required" = "$RUNTIME_PYTHON" ] && [ ! -x "$required" ]; }; then
    fatal config 78 "required launch asset missing"
    exit 78
  fi
done

resolved_env="$(mktemp "${TMPDIR:-/tmp}/hermes-gateway-secrets.XXXXXX")" || exit 78
trap 'rm -f "$resolved_env"' EXIT
"$RUNTIME_PYTHON" "$RESOLVER" "$REFERENCE_FILE" >"$resolved_env" 2>>"$LOG"
resolver_status=$?
case "$resolver_status" in
  0) ;;
  "$EXIT_AUTH")
    fatal permanent-auth "$resolver_status" "credential repair required; parking launchd job"
    exit 0
    ;;
  "$EXIT_TRANSIENT")
    fatal transient-exhausted "$resolver_status" "bounded resolution exhausted; launchd may retry"
    exit "$resolver_status"
    ;;
  *)
    fatal resolver "$resolver_status" "refusing unresolved launch"
    exit "$resolver_status"
    ;;
esac

set -a
# shellcheck disable=SC1090
. "$resolved_env"
set +a

# 1Password's "Hermes Flags" item pre-sets HERMES_AUTONOMOUS_MERGE (master),
# _MEDIUM, and _HIGH to "1" regardless of which tier has actually graduated.
# The master flag enables BOTH low and medium tier per
# merge_guard._tier_autonomy_enabled(), so leaving it resolved would silently
# turn on medium-tier autonomous merge even though medium hasn't graduated yet
# (ClickUp 86e2h2q7z, still pending). Neutralize the flags this preset
# shouldn't govern; HERMES_AUTONOMOUS_MERGE_LOW is deliberately left resolved
# since low tier is the intentionally-live one.
unset HERMES_AUTONOMOUS_MERGE
unset HERMES_AUTONOMOUS_MERGE_MEDIUM
unset HERMES_AUTONOMOUS_MERGE_HIGH

GH_TOKEN="$("$RUNTIME_PYTHON" "$TOKEN_MINTER" 2>>"$LOG")"
mint_status=$?
if [ "$mint_status" -eq "$EXIT_AUTH" ]; then
  fatal permanent-auth "$mint_status" "GitHub App credential repair required; parking launchd job"
  exit 0
fi
if [ "$mint_status" -ne 0 ] || [ -z "$GH_TOKEN" ]; then
  fatal token-mint "${mint_status:-1}" "GitHub App token unavailable; refusing launch"
  exit "$EXIT_TRANSIENT"
fi
export GH_TOKEN
unset GH_APP_PRIVATE_KEY GH_APP_ID GH_APP_INSTALLATION_ID
if [ -z "${OPENAI_API_KEY_HERMES:-}" ]; then
  fatal permanent-auth "$EXIT_AUTH" "OpenAI credential missing; parking launchd job"
  exit 0
fi

printf '%s gateway_secrets_wrap: launch environment ready\n' "$(timestamp)" >>"$LOG"
exec /bin/bash "$INNER"
