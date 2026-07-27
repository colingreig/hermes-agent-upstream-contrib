#!/bin/bash
# Source-controlled launchd bootstrap for the Hermes gateway.  Secret
# resolution retries live in op_sdk_resolve.py; this boundary classifies the
# terminal result and never starts a second unbounded retry loop.
set -uo pipefail

HOME_DIR="$HOME"
TOKEN_FILE="$HOME_DIR/.config/op-runtime-token"
ENV_FILE="$HOME_DIR/.hermes/scripts/op-secrets.env"
RESOLVER="$HOME_DIR/.hermes/scripts/op_sdk_resolve.py"
RESOLVER_PYTHON="$HOME_DIR/.hermes/runtime-current/venv/bin/python"
HERMES_BIN="$HOME_DIR/.local/bin/hermes"
LOG="$HOME_DIR/.hermes/logs/gateway.error.log"

# Must match op_sdk_resolve.py.  75 is the conventional temporary-failure
# status; 77 is a permanent authentication/authorization failure.
EXIT_TRANSIENT=75
EXIT_AUTH=77

mkdir -p "$(dirname "$LOG")"
TS="$(date -u +%FT%TZ)"

if [ ! -x "$RESOLVER_PYTHON" ] || [ ! -f "$RESOLVER" ] \
   || [ ! -f "$TOKEN_FILE" ] || [ ! -f "$ENV_FILE" ] \
   || [ ! -x "$HERMES_BIN" ]; then
  echo "$TS gateway_secrets_wrap: FATAL classification=config: resolver/venv/token/env/hermes executable missing" >> "$LOG"
  exit 78
fi

resolved_env="$(mktemp "${TMPDIR:-/tmp}/hermes-gateway-secrets.XXXXXX")"
trap 'rm -f "$resolved_env"' EXIT

"$RESOLVER_PYTHON" "$RESOLVER" "$ENV_FILE" > "$resolved_env" 2>>"$LOG"
resolver_status=$?
case "$resolver_status" in
  0)
    ;;
  "$EXIT_AUTH")
    echo "$TS gateway_secrets_wrap: FATAL classification=permanent-auth exit=$resolver_status: refusing gateway launch; credentials must be repaired" >> "$LOG"
    exit "$resolver_status"
    ;;
  "$EXIT_TRANSIENT")
    echo "$TS gateway_secrets_wrap: FATAL classification=transient-exhausted exit=$resolver_status: resolver's bounded retries exhausted; refusing unresolved launch" >> "$LOG"
    exit "$resolver_status"
    ;;
  *)
    echo "$TS gateway_secrets_wrap: FATAL classification=resolver exit=$resolver_status: refusing unresolved gateway launch" >> "$LOG"
    exit "$resolver_status"
    ;;
esac

set -a
# shellcheck disable=SC1090
. "$resolved_env"
set +a

echo "$TS gateway_secrets_wrap: secrets resolved; launching gateway" >> "$LOG"
exec "$HERMES_BIN" gateway run --replace
