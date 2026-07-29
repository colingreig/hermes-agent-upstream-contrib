#!/usr/bin/env bash
# Temporary credential helper for private repos not yet granted to the GitHub App.
# Resolves the PAT transiently via op-run. store/erase are no-ops.
set -euo pipefail
[ "${1:-}" = "get" ] || exit 0
TOKEN="$("$HOME/.local/bin/op-run" --env-file "$HOME/.hermes/github-pat.op-env" -- \
  sh -c 'printf %s "$GITHUB_PERSONAL_ACCESS_TOKEN"' 2>/dev/null || true)"
[ -n "$TOKEN" ] || exit 0
printf 'username=colingreig\n'
printf 'password=%s\n' "$TOKEN"
unset TOKEN
