#!/usr/bin/env bash
set -euo pipefail
export IGNITE_SKILLS_REPO="/Users/colingreig/.hermes/releases/ignite-board-sync-14c84fbd86a06f87a6938b5b8edc83453574f68d"
export IGNITE_REPOS_JSON="${IGNITE_REPOS_JSON:-/Users/colingreig/.hermes/releases/ignite-board-sync-14c84fbd86a06f87a6938b5b8edc83453574f68d/skills/ignite-state/references/repos.json}"
export IGNITE_BOARD_POLICY_JSON="${IGNITE_BOARD_POLICY_JSON:-/Users/colingreig/.hermes/releases/ignite-board-sync-14c84fbd86a06f87a6938b5b8edc83453574f68d/skills/ignite-state/references/board-policy.json}"
export IGNITE_BOARD_SYNC_OUTPUT_DIR="${IGNITE_BOARD_SYNC_OUTPUT_DIR:-/Users/colingreig/.cache/ignite-board-sync}"
exec "/Users/colingreig/.hermes/releases/ignite-board-sync-14c84fbd86a06f87a6938b5b8edc83453574f68d/machine-setup/hermes/scripts/ignite-board-sync.sh" "$@"
