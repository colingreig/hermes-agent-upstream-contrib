#!/usr/bin/env bash
# Serve one ephemeral GitHub Actions job, preserve bounded private diagnostics,
# then remove the per-job work tree before systemd starts the next cycle.
set -uo pipefail
umask 077

REPO="${1:?usage: hermes-runner-loop.sh <repo>}"
if [ "${HERMES_RUNNER_TEST_MODE:-0}" = "1" ]; then
  BASE="${HERMES_RUNNER_TEST_HOME:?test home required}"
  HERMES_CI="${HERMES_RUNNER_TEST_ROOT:?test root required}"
else
  BASE="/home/colingreig"
  HERMES_CI="$BASE/.hermes-ci"
fi
DIR="$BASE/actions-runner/$REPO"
PATFILE="$HERMES_CI/reg-pat"
CONFIG="$HERMES_CI/runner-config.json"
SLOTS_DIR="$HERMES_CI/sem/slots"
ARCHIVE_ROOT="$HERMES_CI/diagnostics/$REPO"
FAILURE_ARCHIVE_ROOT="$HERMES_CI/diagnostics-failures/$REPO"
RUNTIME_DIR="$HERMES_CI/runtime"
NAME="hermes-$REPO"
MARK="$SLOTS_DIR/$NAME"

has_job_evidence() {
  [ -f "$RUNTIME_DIR/$NAME.env" ] || return 1
  grep -Eq '^run_id=[^[:space:]]+$' "$RUNTIME_DIR/$NAME.env" \
    && ! grep -Eq '^run_id=(unknown)?$' "$RUNTIME_DIR/$NAME.env" \
    && grep -Eq '^job=[^[:space:]]+$' "$RUNTIME_DIR/$NAME.env" \
    && ! grep -Eq '^job=(unknown)?$' "$RUNTIME_DIR/$NAME.env"
}

has_diag_evidence() {
  [ -d "$DIR/_diag" ] && find "$DIR/_diag" -type f -print -quit | grep -q .
}

reconcile_own_lease() {
  local lease_boot lease_invocation lease_pid current_boot
  [ -f "$MARK" ] || return 0
  lease_boot="$(sed -n 's/^boot_id=//p' "$MARK" | head -n 1)"
  lease_invocation="$(sed -n 's/^invocation_id=//p' "$MARK" | head -n 1)"
  lease_pid="$(sed -n 's/^worker_pid=//p' "$MARK" | head -n 1)"
  current_boot="$(cat /proc/sys/kernel/random/boot_id)"
  if { [ -n "$lease_boot" ] && [ -n "$current_boot" ] && [ "$lease_boot" != "$current_boot" ]; } \
    || { [[ "$lease_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$lease_pid" 2>/dev/null; } \
    || { ! [[ "$lease_pid" =~ ^[0-9]+$ ]] && [ -n "$lease_invocation" ] && [ -n "${INVOCATION_ID:-}" ] && [ "$lease_invocation" != "$INVOCATION_ID" ]; }; then
    rm -f -- "$MARK"
  fi
}

archive_diagnostics() {
  local target_root="${1:?archive root required}"
  local archive_kind="${2:?archive kind required}"
  local stamp archive control_group
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="$target_root/${stamp}-$$"
  mkdir -p "$archive" || return 1
  chmod 0700 "$HERMES_CI" "$(dirname "$target_root")" "$target_root" "$archive" || return 1

  if [ -d "$DIR/_diag" ]; then
    cp -a "$DIR/_diag" "$archive/_diag" || return 1
  fi
  if [ -f "$RUNTIME_DIR/$NAME.env" ]; then
    cp "$RUNTIME_DIR/$NAME.env" "$archive/job.env" || return 1
  fi
  {
    printf 'repo=%s\nrunner_name=%s\narchive_kind=%s\narchived_at=%s\n' "$REPO" "$NAME" "$archive_kind" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'unit=hermes-runner@%s.service\n' "$REPO"
  } > "$archive/runner.env" || return 1

  journalctl -u "hermes-runner@$REPO.service" -n 500 --no-pager \
    > "$archive/journal.log" 2>&1 || true
  control_group="$(systemctl show "hermes-runner@$REPO.service" -p ControlGroup --value 2>/dev/null || true)"
  {
    printf 'control_group=%s\n' "$control_group"
    for file in memory.current memory.events memory.high memory.max memory.pressure cpu.pressure io.pressure; do
      if [ -n "$control_group" ] && [ -r "/sys/fs/cgroup${control_group}/$file" ]; then
        printf '\n[%s]\n' "$file"
        cat "/sys/fs/cgroup${control_group}/$file"
      fi
    done
    for file in /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
      if [ -r "$file" ]; then
        printf '\n[%s]\n' "$file"
        cat "$file"
      fi
    done
  } > "$archive/resources.txt" || return 1
  chmod -R go-rwx "$archive" || return 1
}

prune_archives() {
  local target_root="${1:?archive root required}"
  local days max count old
  days="$(jq -er '.diagnostic_retention_days | select(type == "number" and . == 7)' "$CONFIG")" || return 1
  max="$(jq -er '.diagnostic_max_jobs_per_repo | select(type == "number" and . == 20)' "$CONFIG")" || return 1
  find "$target_root" -mindepth 1 -maxdepth 1 -type d -mtime "+$days" -exec rm -rf -- {} +
  count="$(find "$target_root" -mindepth 1 -maxdepth 1 -type d -print | wc -l)"
  while [ "$count" -gt "$max" ]; do
    old="$(find "$target_root" -mindepth 1 -maxdepth 1 -type d -print | sort | head -n 1)"
    [ -n "$old" ] || break
    rm -rf -- "$old"
    count=$((count - 1))
  done
}

cleanup() {
  # Never destroy the only diagnostic copy. A failed archive leaves the work
  # tree intact for operator recovery and the next cycle exits fail closed.
  if has_job_evidence; then
    if archive_diagnostics "$ARCHIVE_ROOT" job && prune_archives "$ARCHIVE_ROOT"; then
      rm -rf -- "$DIR/_work" "$DIR/_diag"
      rm -f -- "$RUNTIME_DIR/$NAME.env"
    else
      echo "[hermes-ci] job diagnostic preservation failed; workdir retained" >&2
    fi
  elif has_diag_evidence; then
    if archive_diagnostics "$FAILURE_ARCHIVE_ROOT" registration-failure && prune_archives "$FAILURE_ARCHIVE_ROOT"; then
      rm -rf -- "$DIR/_work" "$DIR/_diag"
      rm -f -- "$RUNTIME_DIR/$NAME.env"
    else
      echo "[hermes-ci] failure diagnostic preservation failed; workdir retained" >&2
    fi
  else
    rm -rf -- "$DIR/_work" "$DIR/_diag"
    rm -f -- "$RUNTIME_DIR/$NAME.env" "$MARK"
  fi
  rm -f -- "$SLOTS_DIR/$NAME"
}
trap cleanup EXIT

cd "$DIR" || { echo "no runner dir $DIR"; exit 1; }

# Preserve anything left by a hard-killed cycle before clearing registration.
if [ -d "$DIR/_work" ] || [ -d "$DIR/_diag" ]; then
  if has_job_evidence; then
    archive_diagnostics "$ARCHIVE_ROOT" job || { echo "[hermes-ci] cannot preserve prior job cycle" >&2; exit 1; }
    prune_archives "$ARCHIVE_ROOT" || { echo "[hermes-ci] cannot enforce job diagnostic retention" >&2; exit 1; }
  elif has_diag_evidence; then
    archive_diagnostics "$FAILURE_ARCHIVE_ROOT" registration-failure || { echo "[hermes-ci] cannot preserve prior failure cycle" >&2; exit 1; }
    prune_archives "$FAILURE_ARCHIVE_ROOT" || { echo "[hermes-ci] cannot enforce failure diagnostic retention" >&2; exit 1; }
  fi
fi
rm -rf -- "$DIR/_work" "$DIR/_diag"
rm -f -- "$RUNTIME_DIR/$NAME.env"
rm -f -- "$DIR/.runner" "$DIR/.credentials" "$DIR/.credentials_rsaparams"
reconcile_own_lease

if [ "${HERMES_RUNNER_TEST_MODE:-0}" = "1" ]; then
  case "${HERMES_RUNNER_TEST_ACTION:-}" in
    cleanup)
      cleanup
      trap - EXIT
      exit 0
      ;;
    reconcile-lease)
      trap - EXIT
      exit 0
      ;;
  esac
fi

PAT="$(cat "$PATFILE")"
REG_TOKEN="$(curl -fsS -X POST \
  -H "Authorization: token $PAT" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/colingreig/$REPO/actions/runners/registration-token" \
  | jq -r .token)"
if [ -z "$REG_TOKEN" ] || [ "$REG_TOKEN" = "null" ]; then
  echo "registration-token mint failed for $REPO"
  exit 1
fi

./config.sh \
  --url "https://github.com/colingreig/$REPO" \
  --token "$REG_TOKEN" \
  --labels self-hosted,linux,hermes-mini \
  --ephemeral --unattended --replace \
  --name "$NAME" || { echo "config.sh failed for $REPO"; exit 1; }

./run.sh
exit $?
