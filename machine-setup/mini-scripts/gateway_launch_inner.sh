#!/bin/bash
# Gateway-only public environment derived from resolved secret inputs.
set -uo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
RUNTIME_PYTHON="$HERMES_HOME/runtime-current/venv/bin/python"
LOG="$HERMES_HOME/logs/gateway.error.log"
VALIDATOR_CHAIN="openai-codex:gpt-5.4,minimax:MiniMax-M3,gemini:gemini-3.5-flash"

if [ ! -x "$RUNTIME_PYTHON" ]; then
  printf '%s gateway_launch_inner: FATAL classification=config: runtime Python missing\n' \
    "$(date -u +%FT%TZ)" >>"$LOG"
  exit 78
fi
if [ -z "${OPENAI_API_KEY_HERMES:-}" ]; then
  printf '%s gateway_launch_inner: FATAL classification=config: OpenAI key source missing\n' \
    "$(date -u +%FT%TZ)" >>"$LOG"
  exit 78
fi

export OPENAI_API_KEY="$OPENAI_API_KEY_HERMES"
unset OPENAI_API_KEY_HERMES
export VALIDATOR_LOW_CHAIN="$VALIDATOR_CHAIN"
export VALIDATOR_MEDIUM_CHAIN="$VALIDATOR_CHAIN"
export VALIDATOR_HIGH_CHAIN="$VALIDATOR_CHAIN"

exec "$RUNTIME_PYTHON" -m hermes_cli.main gateway run --replace
