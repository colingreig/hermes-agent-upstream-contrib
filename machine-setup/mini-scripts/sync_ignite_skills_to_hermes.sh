#!/usr/bin/env bash
# verify_ignite_skills_external wiring (formerly sync_ignite_skills_to_hermes.sh).
#
# Long-term policy (2026-08-01, epic 86e2kmq8u): Ignite skills are NOT copied into
# ~/.hermes/skills. That tree is fleet-governed (~23 manifests). Ignite skills live
# in ~/dev/ignite-skills-live and are discovered via skills.external_dirs after
# ignite-skills-pull.sh updates the checkout.
#
# This script verifies external wiring and fails if local Ignite shadows exist.
#
# Usage (on the Hermes Mac mini):
#   bash ~/.hermes/scripts/sync_ignite_skills_to_hermes.sh
#   bash ~/.hermes/scripts/sync_ignite_skills_to_hermes.sh --dry-run   # ignored; always verify-only
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${HERMES_PYTHON:-}"

if [[ -z "$PYTHON" ]]; then
  if [[ -x "${HERMES_HOME:-$HOME/.hermes}/runtime-current/venv/bin/python3" ]]; then
    PYTHON="${HERMES_HOME:-$HOME/.hermes}/runtime-current/venv/bin/python3"
  elif [[ -x "$HOME/dev/hermes-agent/.venv/bin/python3" ]]; then
    PYTHON="$HOME/dev/hermes-agent/.venv/bin/python3"
  else
    PYTHON="python3"
  fi
fi

exec "$PYTHON" "$SCRIPT_DIR/verify_ignite_skills_external.py" "$@"
