#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED: record completion and release the slot.
set -euo pipefail
umask 077

if [ "${HERMES_RUNNER_TEST_MODE:-0}" = "1" ]; then
  BASE="${HERMES_RUNNER_TEST_ROOT:?test root required}"
else
  BASE="/home/colingreig/.hermes-ci"
fi
RUNNER="${RUNNER_NAME:-unknown}"
META="$BASE/runtime/$RUNNER.env"
if [ -f "$META" ]; then
  printf 'job_completed_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$META"
fi
rm -f "$BASE/sem/slots/$RUNNER"
