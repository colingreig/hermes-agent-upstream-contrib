#!/usr/bin/env bash
# Git credential helper for the Hermes Dev Assistant GitHub App.
# MINI DESTINATION: ~/.hermes/scripts/github_app_cred.sh
# Wired up by ~/.hermes/gitconfig (referenced via GIT_CONFIG_GLOBAL).
#
# Secrets are resolved transiently from 1Password via op-run; installation
# tokens are minted per request and are never stored. store/erase are no-ops.
#
# 2026-07-26 (cron-auth outage): the interpreter is now resolved ABSOLUTELY.
# Git invokes this helper with whatever PATH the calling shell had; under the
# terminal tool that PATH has been reordered by /etc/profile's path_helper so
# /usr/local/bin precedes the Hermes venv, and bare `python3` resolved to the
# python.org 3.13 build with no CA bundle -> SSL: CERTIFICATE_VERIFY_FAILED on
# api.github.com -> empty token -> `git push` fell through to an unauthenticated
# HTTPS request. Mint failures are now logged instead of silently discarded.
set -euo pipefail
[ "${1:-}" = "get" ] || exit 0
export GIT_TERMINAL_PROMPT=0

# Absolute interpreter: never trust PATH here (see header).
HERMES_PY="$HOME/.hermes/runtime-current/venv/bin/python"
[ -x "$HERMES_PY" ] || HERMES_PY="/usr/bin/python3"

MINT_LOG="$HOME/.hermes/logs/github-app-token.log"
ERR_FILE="$(mktemp -t hermes-gitcred-mint)"
TOKEN="$("$HOME/.local/bin/op-run" \
  --env-file "$HOME/.hermes/github-app.op-env" -- \
  "$HERMES_PY" "$HOME/.hermes/scripts/github_app_token.py" 2>"$ERR_FILE" || true)"

if [ -z "$TOKEN" ]; then
  printf '%s git-cred-helper: GitHub App token mint FAILED (py=%s): %s\n' \
    "$(date -u +%FT%TZ)" "$HERMES_PY" \
    "$(tr '\n' ' ' <"$ERR_FILE" | tail -c 600)" >>"$MINT_LOG" 2>/dev/null || true
  rm -f "$ERR_FILE"
  exit 0
fi
rm -f "$ERR_FILE"

printf 'username=x-access-token\n'
printf 'password=%s\n' "$TOKEN"
unset TOKEN
