#!/usr/bin/env bash
# Hermes no-agent entry point for the scheduled provider model-catalog check.
set -euo pipefail

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO_ROOT="${IGNITE_SKILLS_REPO:-$HOME/dev/ignite-skills}"
OUTPUT_DIR="${MODEL_DEPRECATION_OUTPUT_DIR:-$HOME/.cache/ignite-model-deprecation}"
NODE_BIN="${NODE_BIN:-node}"

mkdir -p "$OUTPUT_DIR"
tmp_report="$(mktemp "$OUTPUT_DIR/last-run.XXXXXX.json")"
# shellcheck disable=SC2329 # invoked indirectly by trap
cleanup() {
  rm -f "$tmp_report"
}
trap cleanup EXIT

if [ "$#" -eq 0 ]; then
  args=(--json)
else
  args=("$@")
fi

set +e
"$NODE_BIN" "$REPO_ROOT/skills/ignite-state/scripts/model-deprecation-check.mjs" "${args[@]}" >"$tmp_report"
check_status=$?
set -e

if [ -s "$tmp_report" ] && "$NODE_BIN" -e "JSON.parse(require('node:fs').readFileSync(process.argv[1], 'utf8'))" "$tmp_report"; then
  mv "$tmp_report" "$OUTPUT_DIR/last-run.json"
  cat "$OUTPUT_DIR/last-run.json"
else
  printf 'ignite-model-deprecation-check: checker produced no valid JSON report\n' >&2
  exit 1
fi

exit "$check_status"
