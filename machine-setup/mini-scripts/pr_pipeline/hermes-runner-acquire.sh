#!/usr/bin/env bash
# ACTIONS_RUNNER_HOOK_JOB_STARTED: acquire the single shared Hermes CI slot.
set -euo pipefail
umask 077

if [ "${HERMES_RUNNER_TEST_MODE:-0}" = "1" ]; then
  BASE="${HERMES_RUNNER_TEST_ROOT:?test root required}"
else
  BASE="/home/colingreig/.hermes-ci"
fi
CONFIG="$BASE/runner-config.json"
SEM="$BASE/sem"
LOCK="$SEM/sem.lock"
SLOTS_DIR="$SEM/slots"
RUNTIME_DIR="$BASE/runtime"
RUNNER="${RUNNER_NAME:-unknown}"
MARK="$SLOTS_DIR/$RUNNER"
META="$RUNTIME_DIR/$RUNNER.env"

MAX="$(jq -er '.concurrency_slots | select(type == "number" and . == 1)' "$CONFIG")"
TIMEOUT="$(jq -er '.semaphore_timeout_seconds | select(type == "number" and . == 1800)' "$CONFIG")"
mkdir -p "$SLOTS_DIR" "$RUNTIME_DIR"
chmod 0700 "$BASE" "$SEM" "$SLOTS_DIR" "$RUNTIME_DIR"

lease_is_proven_stale() {
  local lease="${1:?lease required}"
  local owner lease_boot lease_invocation lease_pid current_boot
  owner="$(basename "$lease")"
  lease_boot="$(sed -n 's/^boot_id=//p' "$lease" | head -n 1)"
  lease_invocation="$(sed -n 's/^invocation_id=//p' "$lease" | head -n 1)"
  lease_pid="$(sed -n 's/^worker_pid=//p' "$lease" | head -n 1)"
  current_boot="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
  if [ -n "$lease_boot" ] && [ -n "$current_boot" ] && [ "$lease_boot" != "$current_boot" ]; then
    return 0
  fi
  if [[ "$lease_pid" =~ ^[0-9]+$ ]]; then
    kill -0 "$lease_pid" 2>/dev/null && return 1
    return 0
  fi
  [ "$owner" = "$RUNNER" ] \
    && [ -n "$lease_invocation" ] \
    && [ -n "${INVOCATION_ID:-}" ] \
    && [ "$lease_invocation" != "$INVOCATION_ID" ]
}

legacy_empty_lease_is_proven_stale() {
  local lease="${1:?lease required}"
  local owner repo modified now cmdline
  [ ! -s "$lease" ] || return 1
  owner="$(basename "$lease")"
  repo="${owner#hermes-}"
  [ "$repo" != "$owner" ] || return 1

  modified="$(stat -c %Y "$lease" 2>/dev/null || stat -f %m "$lease" 2>/dev/null || true)"
  [[ "$modified" =~ ^[0-9]+$ ]] || return 1
  now="$(date +%s)"
  [ $((now - modified)) -ge "$TIMEOUT" ] || return 1

  # Pre-managed hooks wrote empty marker files. An aged marker is safe to
  # reclaim only when its owning runner has no live Worker process; the
  # listener service itself is intentionally long-lived and is not ownership
  # evidence for a job lease.
  for cmdline in /proc/[0-9]*/cmdline; do
    [ -r "$cmdline" ] || continue
    if tr '\0' ' ' < "$cmdline" 2>/dev/null \
      | grep -Fq "/actions-runner/$repo/bin/Runner.Worker"; then
      return 1
    fi
  done
  return 0
}

reconcile_stale_leases() {
  local lease
  for lease in "$SLOTS_DIR"/*; do
    [ -f "$lease" ] || continue
    if lease_is_proven_stale "$lease" || legacy_empty_lease_is_proven_stale "$lease"; then
      rm -f -- "$lease"
    fi
  done
}

if [ "${HERMES_RUNNER_TEST_ACTION:-}" = "reconcile-leases" ]; then
  (
    flock 9
    reconcile_stale_leases
  ) 9>"$LOCK"
  exit 0
fi

{
  printf 'runner_name=%s\n' "$RUNNER"
  printf 'repository=%s\n' "${GITHUB_REPOSITORY:-unknown}"
  printf 'workflow=%s\n' "${GITHUB_WORKFLOW:-unknown}"
  printf 'run_id=%s\n' "${GITHUB_RUN_ID:-unknown}"
  printf 'run_attempt=%s\n' "${GITHUB_RUN_ATTEMPT:-unknown}"
  printf 'job=%s\n' "${GITHUB_JOB:-unknown}"
  printf 'job_started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$META"
chmod 0600 "$META"

deadline=$(( $(date +%s) + TIMEOUT ))
while :; do
  got=$(
    (
      flock 9
      reconcile_stale_leases
      count="$(find "$SLOTS_DIR" -maxdepth 1 -type f -print | wc -l)"
      if [ "$count" -lt "$MAX" ]; then
        {
          printf 'runner_name=%s\n' "$RUNNER"
          printf 'boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
          printf 'invocation_id=%s\n' "${INVOCATION_ID:-unknown}"
          printf 'worker_pid=%s\n' "$PPID"
          printf 'acquired_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        } > "$MARK"
        chmod 0600 "$MARK"
        echo yes
      else
        echo no
      fi
    ) 9>"$LOCK"
  )
  [ "$got" = "yes" ] && exit 0
  if [ "$(date +%s)" -ge "$deadline" ]; then
    printf '[hermes-ci semaphore] timed out after %ss; acquisition failed closed\n' "$TIMEOUT" >&2
    exit 1
  fi
  sleep 3
done
