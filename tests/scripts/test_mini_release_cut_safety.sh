#!/usr/bin/env bash
# Focused, dependency-free safety checks for scripts/mini-release-cut.sh.
set -euo pipefail

SCRIPT_DIR="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT="$SCRIPT_DIR/../../scripts/mini-release-cut.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/mini-release-cut-test.XXXXXX")"

cleanup() {
  [ "${KEEP_MINI_RELEASE_TEST_ROOT:-0}" = 1 ] || rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

expect_failure() {
  if ( "$@" ) >/dev/null 2>&1; then
    fail "expected failure: $*"
  fi
}

mkdir -p "$TEST_ROOT/home/.hermes/releases"
HERMES_HOME="$(cd -P "$TEST_ROOT/home/.hermes" && pwd -P)"
export HERMES_HOME
# shellcheck disable=SC1090 # SCRIPT is calculated from this test's location.
MINI_RELEASE_CUT_TEST_LIB=1 source "$SCRIPT"

# Environment-controlled arithmetic is validated as bounded plain decimal
# data before any release-state action. Shell-looking payloads remain inert,
# and an invalid prune value cannot delete a release.
NUMERIC_SENTINEL="$HERMES_HOME/releases/v-keep-on-invalid-input"
mkdir -p "$NUMERIC_SENTINEL"
printf 'preserve\n' > "$NUMERIC_SENTINEL/sentinel"
VERIFY_PAYLOAD='1$(touch '"$TEST_ROOT"'/verify-timeout-payload-ran)'
if MINI_RELEASE_VERIFY_TIMEOUT="$VERIFY_PAYLOAD" "$SCRIPT" --dry-run >/dev/null 2>&1; then
  fail "command-substitution-shaped verify timeout was accepted"
fi
[ ! -e "$TEST_ROOT/verify-timeout-payload-ran" ] \
  || fail "verify timeout payload executed"
KEEP_PAYLOAD='0$(touch '"$TEST_ROOT"'/keep-extra-payload-ran)'
if MINI_RELEASE_KEEP_EXTRA="$KEEP_PAYLOAD" "$SCRIPT" --dry-run --prune >/dev/null 2>&1; then
  fail "command-substitution-shaped keep-extra value was accepted"
fi
[ ! -e "$TEST_ROOT/keep-extra-payload-ran" ] || fail "keep-extra payload executed"
[ -f "$NUMERIC_SENTINEL/sentinel" ] || fail "invalid keep-extra input deleted a release"
for bad_verify in 0 901 01 -1; do
  if MINI_RELEASE_VERIFY_TIMEOUT="$bad_verify" "$SCRIPT" --dry-run >/dev/null 2>&1; then
    fail "invalid verify timeout was accepted: $bad_verify"
  fi
done
for bad_drain in 0 901 01 -1; do
  if MINI_RELEASE_DRAIN_TIMEOUT="$bad_drain" "$SCRIPT" --dry-run >/dev/null 2>&1; then
    fail "invalid release drain timeout was accepted: $bad_drain"
  fi
done
for bad_keep in 21 01 -1; do
  if MINI_RELEASE_KEEP_EXTRA="$bad_keep" "$SCRIPT" --dry-run --prune >/dev/null 2>&1; then
    fail "invalid keep-extra value was accepted: $bad_keep"
  fi
done

# The first guarded cut can start from an older runtime. Bootstrap must carry
# the target registry beside the target lease client, or registry validation
# would fail before the new runtime is activated.
grep -Fq 'machine-setup/production_mutation_registry.json' "$SCRIPT" \
  || fail "production write lease bootstrap omits the target registry"
grep -Fq 'active runtime commit for rollback lease' "$SCRIPT" \
  || fail "explicit rollback is not fenced by the production write lease"

# A first deployment can explicitly provide the target lease module before
# runtime-current contains it, but only from a tightly verified /tmp bundle.
BOOTSTRAP_RUNTIME="$TEST_ROOT/bootstrap-runtime"
BOOTSTRAP_OVERRIDE="$TEST_ROOT/production-write-lease-bootstrap"
mkdir -p "$BOOTSTRAP_RUNTIME/venv/bin" "$BOOTSTRAP_OVERRIDE/cron" \
  "$BOOTSTRAP_OVERRIDE/machine-setup"
ln -s "$(command -v python3)" "$BOOTSTRAP_RUNTIME/venv/bin/python"
cp "$SCRIPT_DIR/../../cron/production_write_lease.py" \
  "$BOOTSTRAP_OVERRIDE/cron/production_write_lease.py"
: > "$BOOTSTRAP_OVERRIDE/cron/__init__.py"
cp "$SCRIPT_DIR/../../hermes_constants.py" "$BOOTSTRAP_OVERRIDE/hermes_constants.py"
cp "$SCRIPT_DIR/../../machine-setup/production_mutation_registry.json" \
  "$BOOTSTRAP_OVERRIDE/machine-setup/production_mutation_registry.json"
BOOTSTRAP_CURRENT="$HERMES_HOME/runtime-current"
ln -s "$BOOTSTRAP_RUNTIME" "$BOOTSTRAP_CURRENT"
bootstrap_production_write_lease() { :; } # replaced after negative fixture below

# The real function must reject an override that would execute package code.
if (
  unset -f bootstrap_production_write_lease
  eval "$(sed -n '/^validate_production_write_lease_bootstrap() {/,/^}$/p' "$SCRIPT")"
  PRODUCTION_WRITE_LEASE_PYTHON="$BOOTSTRAP_RUNTIME/venv/bin/python"
  printf 'not empty\n' > "$BOOTSTRAP_OVERRIDE/cron/__init__.py"
  validate_production_write_lease_bootstrap "$BOOTSTRAP_OVERRIDE"
); then
  fail "non-empty lease bootstrap cron init was accepted"
fi
: > "$BOOTSTRAP_OVERRIDE/cron/__init__.py"
# A safe leaf is insufficient if an intermediate directory is a symlink.
# Exercise both package parents, including a symlink that still points inside
# the otherwise-valid override tree.
mv "$BOOTSTRAP_OVERRIDE/cron" "$BOOTSTRAP_OVERRIDE/cron-real"
ln -s cron-real "$BOOTSTRAP_OVERRIDE/cron"
if (
  PRODUCTION_WRITE_LEASE_PYTHON="$BOOTSTRAP_RUNTIME/venv/bin/python"
  validate_production_write_lease_bootstrap "$BOOTSTRAP_OVERRIDE"
); then
  fail "symlinked lease bootstrap cron parent was accepted"
fi
rm "$BOOTSTRAP_OVERRIDE/cron"
mv "$BOOTSTRAP_OVERRIDE/cron-real" "$BOOTSTRAP_OVERRIDE/cron"
mv "$BOOTSTRAP_OVERRIDE/machine-setup" "$BOOTSTRAP_OVERRIDE/machine-setup-real"
ln -s machine-setup-real "$BOOTSTRAP_OVERRIDE/machine-setup"
if (
  PRODUCTION_WRITE_LEASE_PYTHON="$BOOTSTRAP_RUNTIME/venv/bin/python"
  validate_production_write_lease_bootstrap "$BOOTSTRAP_OVERRIDE"
); then
  fail "symlinked lease bootstrap machine-setup parent was accepted"
fi
rm "$BOOTSTRAP_OVERRIDE/machine-setup"
mv "$BOOTSTRAP_OVERRIDE/machine-setup-real" "$BOOTSTRAP_OVERRIDE/machine-setup"
# Invoke the real bootstrap in a fresh sourced helper context; it must select
# and import the override target rather than relying on git archive/current.
unset -f bootstrap_production_write_lease
eval "$(sed -n '/^bootstrap_production_write_lease() {/,/^}$/p' "$SCRIPT")"
HERMES_PRODUCTION_WRITE_LEASE_BOOTSTRAP_DIR="$BOOTSTRAP_OVERRIDE" \
  bootstrap_production_write_lease
[ "$PRODUCTION_WRITE_LEASE_ROOT" = "$BOOTSTRAP_OVERRIDE" ] \
  || fail "valid lease bootstrap override was not selected"
"$PRODUCTION_WRITE_LEASE_PYTHON" - "$PRODUCTION_WRITE_LEASE_ROOT" <<'PY' \
  || fail "valid lease bootstrap override did not import target mutation guard"
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from cron import production_write_lease
assert Path(production_write_lease.__file__).resolve() == root / "cron/production_write_lease.py"
assert callable(production_write_lease.mutation_guard)
PY
cleanup_production_write_lease_bootstrap
[ -d "$BOOTSTRAP_OVERRIDE" ] || fail "caller-owned lease bootstrap override was deleted"
rm "$BOOTSTRAP_CURRENT"
unset HERMES_PRODUCTION_WRITE_LEASE_BOOTSTRAP_DIR

# Normal cut authority binds the production lease to the certified target,
# not the old runtime HEAD used only to bootstrap the lease implementation.
LEASE_TARGET=dddddddddddddddddddddddddddddddddddddddd
LEASE_COMMIT_CAPTURE="$TEST_ROOT/lease-commit-capture"
(
  production_write_lease_call() {
    [ "$1" = acquire ] || return 1
    printf '%s' "$3" > "$LEASE_COMMIT_CAPTURE"
    printf '{"lease_id":"target","actor":"mini-release-cut","session_id":"session","fencing_token":1,"commit_sha":"%s"}\n' "$3"
  }
  acquire_production_write_lease "$LEASE_TARGET"
) >/dev/null
[ "$(cat "$LEASE_COMMIT_CAPTURE")" = "$LEASE_TARGET" ] \
  || fail "normal cut lease was not bound to the certified target SHA"

# A fence failure after the atomic runtime-current swap must NOT invoke a
# stale-owner rollback. It records loss evidence and leaves recovery to the
# successor/operator.
LEASE_NEW="$RELEASES_DIR/v-fence-new"
LEASE_OLD="$RELEASES_DIR/v-fence-old"
mkdir -p "$LEASE_NEW" "$LEASE_OLD"
ln -s "$LEASE_NEW" "$HERMES_HOME/runtime-current"
FENCE_LOSS_OUTPUT="$TEST_ROOT/fence-loss.out"
(
  expected_lease='{"lease_id":"test","actor":"mini-release-cut","session_id":"session","fencing_token":28,"commit_sha":"dddddddddddddddddddddddddddddddddddddddd"}'
  PRODUCTION_WRITE_LEASE_JSON="$expected_lease"
  NEW_DIR="$LEASE_NEW"
  production_write_lease_call() {
    case "$1" in
      heartbeat) return 1 ;;
      fence-loss)
        [ "$2" = "$expected_lease" ] || return 98
        [ "$3" = heartbeat-refused ] || return 99
        printf '{"receipt_id":"fence-loss-28"}\n'
        ;;
      *) return 1 ;;
    esac
  }
  rollback_to_previous() { : > "$TEST_ROOT/rollback-after-fence"; return 0; }
  heartbeat_production_write_lease
) >"$FENCE_LOSS_OUTPUT" 2>&1 && fail "post-swap fence failure unexpectedly succeeded"
[ ! -f "$TEST_ROOT/rollback-after-fence" ] || fail "stale owner rolled back after fence loss"
grep -Fq 'persisted production write fence-loss receipt: {"receipt_id":"fence-loss-28"}' "$FENCE_LOSS_OUTPUT" \
  || fail "post-swap fence loss did not preserve valid lease JSON and report its durable receipt"
(
  expected_lease='{"lease_id":"test","actor":"mini-release-cut","session_id":"session","fencing_token":28,"commit_sha":"dddddddddddddddddddddddddddddddddddddddd"}'
  PRODUCTION_WRITE_LEASE_JSON="$expected_lease"
  PRODUCTION_WRITE_FENCE_LOSS_RECEIPT_JSON=""
  production_write_lease_call() {
    case "$1" in
      heartbeat) return 1 ;;
      fence-loss)
        [ "$2" = "$expected_lease" ] || return 98
        printf '{"receipt_id":"cleanup-loss-28"}\n'
        ;;
      *) return 1 ;;
    esac
  }
  if production_write_mutation_allowed; then
    fail "trap cleanup retained mutation authority after fence loss"
  fi
  [ "$PRODUCTION_WRITE_LEASE_JSON" = "$expected_lease" ] \
    || fail "trap cleanup discarded exact lease identity after fence loss"
  [ "$PRODUCTION_WRITE_FENCE_LOSS_RECEIPT_JSON" = '{"receipt_id":"cleanup-loss-28"}' ] \
    || fail "trap cleanup did not retain durable fence-loss receipt output"
) >/dev/null 2>&1
rm "$HERMES_HOME/runtime-current"

RELEASES_DIR="$(canonical_existing_dir "$TEST_ROOT/home/.hermes/releases")"
PREV_FILE="$RELEASES_DIR/.previous"
CUT_LOCK_DIR="$RELEASES_DIR/.mini-release-cut.lock"
LAST_RECEIPT_FILE="$RELEASES_DIR/.mini-release-last-receipt.json"
REFRESH_BACKUP_FILE="$RELEASES_DIR/.clickup_workspace_refresh.previous"
LOCAL_BIN_DIR="$TEST_ROOT/home/.local/bin"
CLICKUP_CLI_PATH_DIR="$TEST_ROOT/home/homebrew-bin"
# shellcheck disable=SC2034 # referenced by helpers sourced from SCRIPT.
DRY_RUN=0
mkdir -p "$HERMES_HOME/scripts"
printf '#!/usr/bin/env python3\nprint("old")\n' > "$DEPLOYED_REFRESH"
chmod 0755 "$DEPLOYED_REFRESH"

# Fence every rollback boundary. A successor takeover after the pointer
# restoration must prevent every remaining rollback write from this owner.
ROLLBACK_NEW="$RELEASES_DIR/v-rollback-new"
ROLLBACK_OLD="$RELEASES_DIR/v-rollback-old"
mkdir -p "$ROLLBACK_NEW" "$ROLLBACK_OLD"
printf '%s\n' "$ROLLBACK_OLD" > "$PREV_FILE"
ln -s "$ROLLBACK_NEW" "$CURRENT_LINK"
(
  rollback_fences=0
  heartbeat_production_write_lease() {
    rollback_fences=$((rollback_fences + 1))
    [ "$rollback_fences" -lt 2 ] || exit 77
  }
  repoint_symlink() { : > "$TEST_ROOT/rollback-pointer-write"; }
  restore_governed_refresh_for_release() { : > "$TEST_ROOT/rollback-refresh-write"; }
  rollback_to_previous "forced successor takeover"
) >/dev/null 2>&1 && fail "mid-rollback successor takeover unexpectedly succeeded"
[ -f "$TEST_ROOT/rollback-pointer-write" ] || fail "first fenced rollback mutation did not run"
[ ! -f "$TEST_ROOT/rollback-refresh-write" ] || fail "stale rollback mutated after successor takeover"
rm "$CURRENT_LINK"
rm "$PREV_FILE"

# Both the normal cut and rollback call these same service waiters. Simulate a
# wait beyond the 120-second lease TTL without wall-clock sleeping and prove
# each loop renews synchronously rather than relying on a contending background
# writer.
WAIT_RELEASE="$RELEASES_DIR/v-long-verify"
mkdir -p "$WAIT_RELEASE"
ln -s "$WAIT_RELEASE" "$CURRENT_LINK"
(
  VERIFY_TIMEOUT=130
  LEASE_HEARTBEAT_INTERVAL=30
  SECONDS=0
  heartbeat_count=0
  heartbeat_production_write_lease() { heartbeat_count=$((heartbeat_count + 1)); }
  pgrep() { return 1; }
  gateway_platforms_ready() { return 1; }
  port_listening() { return 1; }
  sleep() { SECONDS=$((SECONDS + $1)); }
  if verify_gateway "$WAIT_RELEASE" 0; then
    fail "gateway long-wait fixture unexpectedly verified"
  fi
  [ "$heartbeat_count" -ge 5 ] \
    || fail "gateway verifier did not renew through a wait beyond the lease TTL"
) >/dev/null 2>&1
(
  VERIFY_TIMEOUT=130
  LEASE_HEARTBEAT_INTERVAL=30
  SECONDS=0
  heartbeat_count=0
  heartbeat_production_write_lease() { heartbeat_count=$((heartbeat_count + 1)); }
  http_ok() { return 1; }
  sleep() { SECONDS=$((SECONDS + $1)); }
  if verify_dashboard; then
    fail "dashboard long-wait fixture unexpectedly verified"
  fi
  [ "$heartbeat_count" -ge 5 ] \
    || fail "dashboard verifier did not renew through a wait beyond the lease TTL"
) >/dev/null 2>&1
rm "$CURRENT_LINK"

# A release cut uses the reversible external marker, waits for the gateway's
# fresh aggregate status to reach draining/zero, and renews its production
# write lease throughout the wait. The marker remains armed until the newly
# registered gateway is ready to have admission restored.
DRAIN_RELEASE="$RELEASES_DIR/v-release-drain"
mkdir -p "$DRAIN_RELEASE/venv/bin"
ln -s "$(command -v python3)" "$DRAIN_RELEASE/venv/bin/python"
ln -s "$DRAIN_RELEASE" "$CURRENT_LINK"
DRAIN_REQUEST_FILE="$HERMES_HOME/.drain_request.json"
GATEWAY_STATE_FILE="$HERMES_HOME/gateway_state.json"
(
  DRY_RUN=0
  DRAIN_TIMEOUT=8
  LEASE_HEARTBEAT_INTERVAL=2
  RELEASE_DRAIN_ARMED=0
  SECONDS=0
  heartbeat_count=0
  guarded_production_write() {
    case "${3:-}" in
      *write_drain_request*) printf '{"action":"drain"}\n' > "$DRAIN_REQUEST_FILE" ;;
      *clear_drain_request*) rm -f "$DRAIN_REQUEST_FILE" ;;
      *) return 91 ;;
    esac
  }
  heartbeat_production_write_lease() { heartbeat_count=$((heartbeat_count + 1)); }
  sleep() {
    SECONDS=$((SECONDS + $1))
    case "$SECONDS" in
      1) printf '{"gateway_state":"running","active_agents":0}\n' > "$GATEWAY_STATE_FILE" ;;
      2) printf '{"gateway_state":"draining","active_agents":2}\n' > "$GATEWAY_STATE_FILE" ;;
      3) printf '{"gateway_state":"draining","active_agents":0}\n' > "$GATEWAY_STATE_FILE" ;;
    esac
  }
  begin_release_drain || fail "release drain did not accept fresh draining/zero aggregate status"
  [ "$RELEASE_DRAIN_ARMED" -eq 1 ] || fail "successful wait did not retain drain through registration"
  [ "$heartbeat_count" -ge 2 ] || fail "release drain wait did not renew the production write lease"
  clear_release_drain || fail "release drain marker cleanup failed"
  [ ! -e "$DRAIN_REQUEST_FILE" ] || fail "release drain marker survived successful cleanup"
) >/dev/null

# A gateway that never acknowledges draining/zero times out without an
# unbounded wait; the caller can then remove the marker before any switch.
(
  DRY_RUN=0
  DRAIN_TIMEOUT=3
  LEASE_HEARTBEAT_INTERVAL=1
  RELEASE_DRAIN_ARMED=0
  SECONDS=0
  guarded_production_write() {
    case "${3:-}" in
      *write_drain_request*) printf '{"action":"drain"}\n' > "$DRAIN_REQUEST_FILE" ;;
      *clear_drain_request*) rm -f "$DRAIN_REQUEST_FILE" ;;
      *) return 91 ;;
    esac
  }
  heartbeat_production_write_lease() { :; }
  sleep() {
    SECONDS=$((SECONDS + $1))
    printf '{"gateway_state":"draining","active_agents":1}\n' > "$GATEWAY_STATE_FILE"
  }
  if begin_release_drain; then
    fail "release drain timeout fixture unexpectedly quiesced"
  fi
  [ "$SECONDS" -ge "$DRAIN_TIMEOUT" ] || fail "release drain returned before its bounded deadline"
  clear_release_drain || fail "timed-out release drain could not be cancelled"
  [ ! -e "$DRAIN_REQUEST_FILE" ] || fail "timed-out release drain left its marker armed"
) >/dev/null 2>&1
rm -f "$GATEWAY_STATE_FILE"
rm "$CURRENT_LINK"

# The managed ClickUp wrapper is installed atomically, is executable, and a
# later cut repairs a stale or missing command with the release-owned source.
mkdir -p "$LOCAL_BIN_DIR" "$CLICKUP_CLI_PATH_DIR"
CLI_RELEASE="$RELEASES_DIR/v1.0.0-clickup"
mkdir -p "$CLI_RELEASE/scripts"
printf '#!/usr/bin/env bash\nprintf first\n' > "$CLI_RELEASE/scripts/cu-clickup"
chmod 0755 "$CLI_RELEASE/scripts/cu-clickup"
install_clickup_cli "$CLI_RELEASE"
[ -x "$LOCAL_BIN_DIR/cu-clickup" ] || fail "managed ClickUp CLI was not installed executable"
cmp -s "$CLI_RELEASE/scripts/cu-clickup" "$LOCAL_BIN_DIR/cu-clickup" \
  || fail "managed ClickUp CLI differs from release source"
[ -L "$CLICKUP_CLI_PATH_DIR/cu-clickup" ] \
  || fail "managed ClickUp CLI PATH link was not installed"
[ "$(readlink "$CLICKUP_CLI_PATH_DIR/cu-clickup")" = "$(canonical_existing_dir "$LOCAL_BIN_DIR")/cu-clickup" ] \
  || fail "managed ClickUp CLI PATH link has the wrong target"
printf '#!/usr/bin/env bash\nprintf stale\n' > "$LOCAL_BIN_DIR/cu-clickup"
install_clickup_cli "$CLI_RELEASE"
cmp -s "$CLI_RELEASE/scripts/cu-clickup" "$LOCAL_BIN_DIR/cu-clickup" \
  || fail "managed ClickUp CLI was not repaired from release source"

# The version grammar accepts ordinary PEP 440-compatible values and rejects
# paths, control/whitespace, shell punctuation, and option-looking values.
for version in '1.2.3' '2!1.0rc1+local.1' '1.0-dev_2'; do
  valid_release_version "$version" || fail "valid version rejected: $version"
done
newline_version=$'1.0\nnext'
# shellcheck disable=SC2016 # the literal shell punctuation is the test input.
for version in '' '.' '..' '../escape' '-1.2' '1 2' "$newline_version" '1;rm' '1$(touch)'; do
  if valid_release_version "$version"; then
    fail "unsafe version accepted: $version"
  fi
done

# A lexical traversal cannot satisfy the canonical-parent guard, and a
# symlinked rollback record cannot redirect an otherwise safe release write.
expect_failure assert_release_target "$RELEASES_DIR/../outside"
mkdir -p "$TEST_ROOT/outside"
ln -s "$TEST_ROOT/outside" "$PREV_FILE"
expect_failure assert_regular_release_file "$PREV_FILE"
rm "$PREV_FILE"

# Owner-file locking rejects a live second owner and releases only the exact
# owner bytes through the guarded path.
(
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"owner","actor":"mini-release-cut","session_id":"one","fencing_token":1,"commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","resources":["governed-mini-scripts","runtime-release"]}'
  guarded_production_write() { "$@"; }
  production_write_lease_call() { return 1; }
  acquire_cut_lock
  expect_failure acquire_cut_lock
  release_cut_lock
)
[ ! -e "$CUT_LOCK_DIR" ] || fail "release-cut lock was not removed"

# A failed guarded lock removal or lease release must retain its exact
# in-memory ownership evidence instead of reporting a false cleanup.
(
  CUT_LOCK_OWNER_JSON='{"schema_version":1,"lease":{"lease_id":"still-owned"}}'
  printf '%s' "$CUT_LOCK_OWNER_JSON" > "$CUT_LOCK_DIR"
  chmod 0600 "$CUT_LOCK_DIR"
  LOCK_HELD=1
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"still-owned"}'
  guarded_production_write() { return 1; }
  if release_cut_lock; then
    fail "failed guarded cut-lock removal reported success"
  fi
  [ "$LOCK_HELD" -eq 1 ] || fail "failed cut-lock removal cleared in-memory ownership"
  [ -f "$CUT_LOCK_DIR" ] || fail "failed cut-lock removal removed durable ownership evidence"
) >/dev/null 2>&1
rm "$CUT_LOCK_DIR"
(
  stale='{"lease_id":"old","actor":"mini-release-cut","session_id":"old-session","fencing_token":4,"commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","resources":["governed-mini-scripts","runtime-release"]}'
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"new","actor":"mini-release-cut","session_id":"new-session","fencing_token":5,"commit_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","resources":["governed-mini-scripts","runtime-release"]}'
  printf '{"lease":%s,"schema_version":1}' "$stale" > "$CUT_LOCK_DIR"
  chmod 0600 "$CUT_LOCK_DIR"
  production_write_lease_call() {
    [ "$1" = lock-recovery-proof ] || return 1
    printf '%s' "$3" | grep -Fq '"fencing_token":4' || return 1
    printf '{"authorized":true,"stale_fencing_token":4,"successor_fencing_token":5}\n'
  }
  guarded_production_write() { "$@"; }
  acquire_cut_lock
  grep -Fq '"fencing_token":5' "$CUT_LOCK_DIR" \
    || fail "successor recovery did not replace stale lock with its exact owner metadata"
  release_cut_lock
) >/dev/null
[ ! -e "$CUT_LOCK_DIR" ] || fail "successor-recovered cut lock was not released"

# The one-time legacy mkdir-lock migration requires a current successor proof,
# an exact safe/empty inode snapshot, and no other cutter process. It then
# replaces the directory with the successor's regular owner file.
(
  mkdir "$CUT_LOCK_DIR"
  PRODUCTION_WRITE_LEASE_PYTHON="$SCRIPT_DIR/../../.venv/bin/python"
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"legacy-successor","actor":"mini-release-cut","session_id":"legacy-new","fencing_token":12,"commit_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","resources":["governed-mini-scripts","runtime-release"]}'
  production_write_lease_call() {
    [ "$1" = legacy-lock-recovery-proof ] || return 1
    [[ "$3" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '{"authorized":true,"predecessor_fencing_token":11,"successor_fencing_token":12}\n'
  }
  guarded_production_write() { "$@"; }
  acquire_cut_lock
  [ -f "$CUT_LOCK_DIR" ] && [ ! -L "$CUT_LOCK_DIR" ] \
    || fail "legacy lock migration did not install a regular successor owner file"
  grep -Fq '"fencing_token":12' "$CUT_LOCK_DIR" \
    || fail "legacy lock migration did not bind the successor lease"
  release_cut_lock
) >/dev/null
[ ! -e "$CUT_LOCK_DIR" ] || fail "legacy-migrated cut lock was not released"
# A same-user tool may mention the cutter path as data. Only a shell whose
# executed script argument is mini-release-cut.sh counts as another cutter.
(
  mkdir "$CUT_LOCK_DIR"
  PRODUCTION_WRITE_LEASE_PYTHON="$SCRIPT_DIR/../../.venv/bin/python"
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"mention-successor","actor":"mini-release-cut","session_id":"mention-new","fencing_token":13,"commit_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","resources":["governed-mini-scripts","runtime-release"]}'
  production_write_lease_call() {
    [ "$1" = legacy-lock-recovery-proof ] || return 1
    printf '{"authorized":true}\n'
  }
  guarded_production_write() { "$@"; }
  mention_tool="$TEST_ROOT/tool/shellcheck"
  mkdir -p "$(dirname "$mention_tool")"
  printf '%s\n' '#!/bin/bash' 'while :; do sleep 1; done' > "$mention_tool"
  chmod 0700 "$mention_tool"
  "$mention_tool" "$SCRIPT" &
  mention_pid=$!
  noexec_script="$TEST_ROOT/parse-only/mini-release-cut.sh"
  mkdir -p "$(dirname "$noexec_script")"
  mkfifo "$noexec_script"
  /bin/bash -n "$noexec_script" &
  noexec_pid=$!
  trap 'kill "$mention_pid" "$noexec_pid" 2>/dev/null || true; wait "$mention_pid" "$noexec_pid" 2>/dev/null || true' EXIT
  sleep 0.1
  acquire_cut_lock
  release_cut_lock
) >/dev/null || fail "shellcheck-style cutter-path mention blocked legacy lock migration"
[ ! -e "$CUT_LOCK_DIR" ] || fail "mention-safe legacy cut lock was not released"
(
  mkdir "$CUT_LOCK_DIR"
  printf 'not empty\n' > "$CUT_LOCK_DIR/sentinel"
  PRODUCTION_WRITE_LEASE_PYTHON="$SCRIPT_DIR/../../.venv/bin/python"
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"unsafe-successor","actor":"mini-release-cut","session_id":"unsafe-new","fencing_token":14,"commit_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","resources":["governed-mini-scripts","runtime-release"]}'
  production_write_lease_call() {
    [ "$1" = legacy-lock-recovery-proof ] || return 1
    printf '{"authorized":true}\n'
  }
  guarded_production_write() { "$@"; }
  acquire_cut_lock
) >/dev/null 2>&1 && fail "non-empty legacy lock directory was migrated"
[ -f "$CUT_LOCK_DIR/sentinel" ] || fail "unsafe legacy lock directory was mutated"
rm "$CUT_LOCK_DIR/sentinel"
rmdir "$CUT_LOCK_DIR"
# A failed or malformed native process enumeration cannot authorize rmdir.
(
  mkdir "$CUT_LOCK_DIR"
  PRODUCTION_WRITE_LEASE_PYTHON="$SCRIPT_DIR/../../.venv/bin/python"
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"ps-failure-successor","actor":"mini-release-cut","session_id":"ps-failure-new","fencing_token":15,"commit_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","resources":["governed-mini-scripts","runtime-release"]}'
  production_write_lease_call() {
    [ "$1" = legacy-lock-recovery-proof ] || return 1
    printf '{"authorized":true}\n'
  }
  guarded_production_write() { "$@"; }
  failed_ps="$TEST_ROOT/tool/ps-failure"
  printf '%s\n' '#!/bin/bash' 'exit 42' > "$failed_ps"
  chmod 0700 "$failed_ps"
  LEGACY_LOCK_PS="$failed_ps"
  acquire_cut_lock
) >/dev/null 2>&1 && fail "failed process enumeration authorized legacy lock migration"
[ -d "$CUT_LOCK_DIR" ] || fail "process-enumeration failure removed legacy lock directory"
rmdir "$CUT_LOCK_DIR"
(
  mkdir "$CUT_LOCK_DIR"
  PRODUCTION_WRITE_LEASE_PYTHON="$SCRIPT_DIR/../../.venv/bin/python"
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"live-process-successor","actor":"mini-release-cut","session_id":"live-process-new","fencing_token":16,"commit_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","resources":["governed-mini-scripts","runtime-release"]}'
  production_write_lease_call() {
    [ "$1" = legacy-lock-recovery-proof ] || return 1
    printf '{"authorized":true}\n'
  }
  guarded_production_write() { "$@"; }
  other_cutter="$TEST_ROOT/other/mini-release-cut.sh"
  mkdir -p "$(dirname "$other_cutter")"
  printf '%s\n' '#!/bin/bash' 'while :; do sleep 1; done' > "$other_cutter"
  chmod 0700 "$other_cutter"
  "$other_cutter" &
  other_cutter_pid=$!
  trap 'kill "$other_cutter_pid" 2>/dev/null || true; wait "$other_cutter_pid" 2>/dev/null || true' EXIT
  acquire_cut_lock
) >/dev/null 2>&1 && fail "legacy lock migrated while another cutter process was live"
[ -d "$CUT_LOCK_DIR" ] || fail "live-cutter refusal removed the legacy lock directory"
rmdir "$CUT_LOCK_DIR"
(
  live='{"lease_id":"live","actor":"mini-release-cut","session_id":"live-session","fencing_token":8,"commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","resources":["governed-mini-scripts","runtime-release"]}'
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"older","actor":"mini-release-cut","session_id":"older-session","fencing_token":7,"commit_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","resources":["governed-mini-scripts","runtime-release"]}'
  printf '{"lease":%s,"schema_version":1}' "$live" > "$CUT_LOCK_DIR"
  chmod 0600 "$CUT_LOCK_DIR"
  production_write_lease_call() { return 1; }
  guarded_production_write() { "$@"; }
  acquire_cut_lock
) >/dev/null 2>&1 && fail "older/successor owner removed a live newer cut lock"
[ -f "$CUT_LOCK_DIR" ] || fail "refused live-owner cut lock was removed"
rm "$CUT_LOCK_DIR"
(
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"still-owned"}'
  production_write_lease_call() { return 1; }
  if release_production_write_lease; then
    fail "failed lease release reported success"
  fi
  [ "$PRODUCTION_WRITE_LEASE_JSON" = '{"lease_id":"still-owned"}' ] \
    || fail "failed lease release discarded exact ownership evidence"
) >/dev/null 2>&1

# Conditional polling accepts equality as a no-op, accepts only a strict
# descendant as an advance, and distinguishes behind/diverged rejection.
classify_fixture() {
  local active="$1" target="$2" mode="$3"
  (
    # shellcheck disable=SC2329 # invoked indirectly by classify_ref_advancement.
    git_current() {
      [ "${1:-}" = merge-base ] || return 2
      [ "${2:-}" = --is-ancestor ] || return 2
      case "$mode:${3:-}:${4:-}" in
        advance:active:target|behind:target:active) return 0 ;;
        *) return 1 ;;
      esac
    }
    classify_ref_advancement "$active" "$target"
  )
}
[ "$(classify_fixture same same equal)" = equal ] || fail "equal commits were not a no-op"
[ "$(classify_fixture active target advance)" = advance ] || fail "strict descendant was not accepted"
[ "$(classify_fixture active target behind)" = behind ] || fail "behind ref was not rejected distinctly"
[ "$(classify_fixture active target diverged)" = diverged ] || fail "diverged ref was not rejected distinctly"

# A dry cut has no release directory to inspect. Governed source validation
# must therefore use only the target commit's ls-tree metadata, accept either
# regular-file mode, and fail closed for every other tree entry shape.
tree_metadata_fixture() {
  local fixture_entry="$1" path="$2"
  (
    SHA=cccccccccccccccccccccccccccccccccccccccc
    # shellcheck disable=SC2034 # consumed by dry_run_target_regular_file_metadata.
    DRY_RUN=1
    # shellcheck disable=SC2329 # invoked indirectly by metadata helper.
    git_current() {
      [ "${1:-}" = ls-tree ] && [ "${2:-}" = "$SHA" ] \
        && [ "${3:-}" = -- ] && [ "${4:-}" = "$path" ] || return 2
      printf '%s\n' "$fixture_entry"
    }
    dry_run_target_regular_file_metadata "$path"
  )
}
TREE_BLOB=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
TREE_PATH="$VENDORED_REFRESH_REL"
tree_metadata_fixture "100644 blob $TREE_BLOB	$TREE_PATH" "$TREE_PATH" \
  || fail "100644 target-tree source was rejected"
tree_metadata_fixture "100755 blob $TREE_BLOB	$TREE_PATH" "$TREE_PATH" \
  || fail "100755 target-tree source was rejected"
expect_failure tree_metadata_fixture '' "$TREE_PATH"
expect_failure tree_metadata_fixture "120000 blob $TREE_BLOB	$TREE_PATH" "$TREE_PATH"
expect_failure tree_metadata_fixture "040000 tree $TREE_BLOB	$TREE_PATH" "$TREE_PATH"
expect_failure tree_metadata_fixture "100644 blob malformed"$'\t'"$TREE_PATH" "$TREE_PATH"
expect_failure tree_metadata_fixture \
  "100644 blob $TREE_BLOB"$'\t'"$TREE_PATH"$'\n'"100644 blob $TREE_BLOB"$'\t'"$TREE_PATH" \
  "$TREE_PATH"
expect_failure tree_metadata_fixture "100644 blob $TREE_BLOB"$'\t'"$TREE_PATH.unexpected" "$TREE_PATH"

# Receipts are deterministic and content-addressed: repeating the same no-op
# state reuses one immutable payload and updates the stable last pointer.
# shellcheck disable=SC2034 # consumed by write_release_receipt from sourced script.
REF="prod-live-patches"
SOURCE_HASH="1111111111111111111111111111111111111111111111111111111111111111"
DEPLOYED_HASH="2222222222222222222222222222222222222222222222222222222222222222"
write_release_receipt noop aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$RELEASES_DIR/v1" "$SOURCE_HASH" "$DEPLOYED_HASH" \
  "resolved ref already active" >/dev/null
first_receipt="$(find "$RELEASES_DIR" -maxdepth 1 -type f -name '.mini-release-receipt-*.json' -print)"
[ -n "$first_receipt" ] || fail "content-addressed receipt was not created"
receipt_count_before="$(find "$RELEASES_DIR" -maxdepth 1 -type f -name '.mini-release-receipt-*.json' | wc -l | tr -d ' ')"
write_release_receipt noop aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$RELEASES_DIR/v1" "$SOURCE_HASH" "$DEPLOYED_HASH" \
  "resolved ref already active" >/dev/null
receipt_count_after="$(find "$RELEASES_DIR" -maxdepth 1 -type f -name '.mini-release-receipt-*.json' | wc -l | tr -d ' ')"
[ "$receipt_count_before" = "$receipt_count_after" ] || fail "idempotent no-op created duplicate receipts"
receipt_name_hash="${first_receipt##*.mini-release-receipt-}"
receipt_name_hash="${receipt_name_hash%.json}"
[ "$(sha256_file "$first_receipt")" = "$receipt_name_hash" ] \
  || fail "receipt filename is not its content SHA-256"
cmp -s "$first_receipt" "$LAST_RECEIPT_FILE" || fail "stable receipt pointer differs from addressed receipt"

# An equal-target no-op is permitted only when an immutable exact-target
# cut/advanced/rollback receipt proves the full activation gate set. A prior
# noop (the state left by the production incident) is not certification.
FULL_TARGET=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
FULL_RUNTIME="$RELEASES_DIR/v-full-activation"
mkdir -p "$FULL_RUNTIME/$(dirname "$VENDORED_REFRESH_REL")" "$(dirname "$DEPLOYED_REFRESH")"
printf '#!/usr/bin/env python3\nprint("full activation")\n' > "$FULL_RUNTIME/$VENDORED_REFRESH_REL"
cp "$FULL_RUNTIME/$VENDORED_REFRESH_REL" "$DEPLOYED_REFRESH"
FULL_HASH="$(sha256_file "$FULL_RUNTIME/$VENDORED_REFRESH_REL")"
FULL_RECEIPT_ID="$(python3 - "$RELEASES_DIR" "$FULL_RUNTIME" "$FULL_TARGET" "$FULL_HASH" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

releases = Path(sys.argv[1])
runtime = sys.argv[2]
target = sys.argv[3]
refresh_hash = sys.argv[4]
payload = {
    "schema_version": 2,
    "event": "advanced",
    "ref": "prod-live-patches",
    "from_commit": "a" * 40,
    "to_commit": target,
    "certified_source_commit": target,
    "promotion_authority_receipt_id": "1" * 64,
    "runtime_target": runtime,
    "refresh_source_sha256": refresh_hash,
    "refresh_deployed_sha256": refresh_hash,
    "pr_pipeline_reconciliation_receipt_id": "3" * 64,
    "review_poll_gate_smoke": "passed",
    "production_write_lease": {
        "actor": "mini-release-cut",
        "commit_sha": target,
        "resources": ["governed-mini-scripts", "runtime-release"],
    },
    "prior_full_activation_receipt_id": None,
    "detail": "full activation fixture",
}
encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
receipt_id = hashlib.sha256(encoded).hexdigest()
(releases / f".mini-release-receipt-{receipt_id}.json").write_bytes(encoded)
print(receipt_id)
PY
)"
[ "$(find_full_activation_receipt "$FULL_TARGET" "$FULL_RUNTIME")" = "$FULL_RECEIPT_ID" ] \
  || fail "exact full-activation receipt was not accepted"
python3 - "$RELEASES_DIR/.mini-release-receipt-$FULL_RECEIPT_ID.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["production_write_lease"]["commit_sha"] = "d" * 40
encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
bad_id = hashlib.sha256(encoded).hexdigest()
path.with_name(f".mini-release-receipt-{bad_id}.json").write_bytes(encoded)
PY
# The valid receipt remains authoritative; remove it momentarily to prove the
# mismatched lease-bound candidate cannot substitute for target authority.
mv "$RELEASES_DIR/.mini-release-receipt-$FULL_RECEIPT_ID.json" \
  "$RELEASES_DIR/.valid-full-receipt.saved"
if find_full_activation_receipt "$FULL_TARGET" "$FULL_RUNTIME" >/dev/null; then
  fail "activation receipt with a non-target lease commit was accepted"
fi
mv "$RELEASES_DIR/.valid-full-receipt.saved" \
  "$RELEASES_DIR/.mini-release-receipt-$FULL_RECEIPT_ID.json"
(
  verify_active_release_health() { return 0; }
  write_equal_target_noop "$FULL_RECEIPT_ID" "$FULL_TARGET" "$FULL_TARGET" "$FULL_RUNTIME"
) >/dev/null
python3 - "$LAST_RECEIPT_FILE" "$FULL_RECEIPT_ID" <<'PY' \
  || fail "no-op receipt did not retain its full-activation authority"
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["event"] == "noop"
assert payload["prior_full_activation_receipt_id"] == sys.argv[2]
PY

NOOP_COUNT_BEFORE="$(find "$RELEASES_DIR" -maxdepth 1 -type f -name '.mini-release-receipt-*.json' | wc -l | tr -d ' ')"
printf 'drifted deployed bytes\n' > "$DEPLOYED_REFRESH"
if (
  verify_active_release_health() { return 0; }
  write_equal_target_noop "$FULL_RECEIPT_ID" "$FULL_TARGET" "$FULL_TARGET" "$FULL_RUNTIME"
) >/dev/null 2>&1; then
  fail "equal-target drift wrote a false no-op receipt"
fi
NOOP_COUNT_AFTER="$(find "$RELEASES_DIR" -maxdepth 1 -type f -name '.mini-release-receipt-*.json' | wc -l | tr -d ' ')"
[ "$NOOP_COUNT_BEFORE" = "$NOOP_COUNT_AFTER" ] \
  || fail "equal-target drift changed immutable receipt inventory"
cp "$FULL_RUNTIME/$VENDORED_REFRESH_REL" "$DEPLOYED_REFRESH"
NOOP_COUNT_BEFORE="$(find "$RELEASES_DIR" -maxdepth 1 -type f -name '.mini-release-receipt-*.json' | wc -l | tr -d ' ')"
if (
  verify_active_release_health() { return 1; }
  write_equal_target_noop "$FULL_RECEIPT_ID" "$FULL_TARGET" "$FULL_TARGET" "$FULL_RUNTIME"
) >/dev/null 2>&1; then
  fail "equal-target unhealthy services wrote a false no-op receipt"
fi
NOOP_COUNT_AFTER="$(find "$RELEASES_DIR" -maxdepth 1 -type f -name '.mini-release-receipt-*.json' | wc -l | tr -d ' ')"
[ "$NOOP_COUNT_BEFORE" = "$NOOP_COUNT_AFTER" ] \
  || fail "equal-target unhealthy services changed immutable receipt inventory"

ln -s "$FULL_RUNTIME" "$CURRENT_LINK"
mkdir -p "$(dirname "$GATEWAY_LOG")"
# Exercise the real current-start boundary derivation against a live PID whose
# argv uses runtime-current and whose PID/start fingerprint matches gateway.pid.
(
  ln -s "$SCRIPT_DIR/../../.venv" "$FULL_RUNTIME/venv"
  "$CURRENT_LINK/venv/bin/python" -c 'import time; time.sleep(30)' gateway run &
  fixture_gateway_pid=$!
  trap 'kill "$fixture_gateway_pid" 2>/dev/null || true; wait "$fixture_gateway_pid" 2>/dev/null || true' EXIT
  sleep 0.1
  "$CURRENT_LINK/venv/bin/python" - "$fixture_gateway_pid" "$HERMES_HOME/gateway.pid" \
    "$GATEWAY_LOG" "$TEST_ROOT/current-start-offset" <<'PY'
import datetime
import json
from pathlib import Path
import sys

import psutil

pid = int(sys.argv[1])
pid_file, log_file, expected_file = map(Path, sys.argv[2:])
created = psutil.Process(pid).create_time()
pid_file.write_text(json.dumps({
    "pid": pid,
    "kind": "hermes-gateway",
    "argv": ["gateway", "run"],
    "start_time": int(round(created * 100)),
}), encoding="utf-8")
pid_file.chmod(0o600)
old = b"2026-01-01 00:00:00,000 INFO gateway.run: Gateway running with 2 platform(s)\n"
current_time = datetime.datetime.fromtimestamp(created + 0.01).strftime(
    "%Y-%m-%d %H:%M:%S,%f"
)[:-3]
log_file.write_bytes(old + (
    f"{current_time} INFO gateway.run: Gateway running with 1 platform(s)\n"
).encode())
expected_file.write_text(str(len(old)), encoding="utf-8")
PY
  [ "$(current_gateway_start_log_offset)" = "$(cat "$TEST_ROOT/current-start-offset")" ] \
    || fail "live PID/start fingerprint did not derive the current gateway log boundary"
) || fail "current gateway start boundary fixture failed"
rm "$HERMES_HOME/gateway.pid"
printf '%s\n' '2026-08-02 10:00:00,000 INFO gateway.run: Gateway running with 2 platform(s)' \
  > "$GATEWAY_LOG"
CURRENT_START_OFFSET="$(wc -c < "$GATEWAY_LOG" | tr -d ' ')"
printf '%s\n' '2026-08-02 10:01:00,000 INFO gateway.run: Gateway running with 1 platform(s)' \
  >> "$GATEWAY_LOG"
if (
  git() { printf '%s\n' "$FULL_TARGET"; }
  pgrep() { return 0; }
  current_gateway_start_log_offset() { printf '%s\n' "$CURRENT_START_OFFSET"; }
  port_listening() { return 0; }
  gateway_health_http() { return 0; }
  http_ok() { return 0; }
  verify_active_release_health "$FULL_TARGET" "$FULL_RUNTIME"
) >/dev/null 2>&1; then
  fail "stale old 2-platform readiness masked a current 1-platform gateway"
fi
if (
  git() { printf '%s\n' "$FULL_TARGET"; }
  pgrep() { return 0; }
  current_gateway_start_log_offset() { return 1; }
  port_listening() { return 0; }
  gateway_health_http() { return 0; }
  http_ok() { return 0; }
  verify_active_release_health "$FULL_TARGET" "$FULL_RUNTIME"
) >/dev/null 2>&1; then
  fail "equal-target health accepted an unverifiable current-start boundary"
fi
printf '%s\n' '2026-08-02 10:01:01,000 INFO gateway.run: Gateway running with 2 platform(s)' \
  >> "$GATEWAY_LOG"
(
  git() { printf '%s\n' "$FULL_TARGET"; }
  pgrep() { return 0; }
  current_gateway_start_log_offset() { printf '%s\n' "$CURRENT_START_OFFSET"; }
  port_listening() { return 0; }
  gateway_health_http() { return 0; }
  http_ok() { return 0; }
  verify_active_release_health "$FULL_TARGET" "$FULL_RUNTIME"
) || fail "current 2-platform readiness was rejected"
if (
  git() { printf '%s\n' "$FULL_TARGET"; }
  pgrep() { return 0; }
  current_gateway_start_log_offset() { printf '%s\n' "$CURRENT_START_OFFSET"; }
  port_listening() { return 0; }
  gateway_health_http() { return 0; }
  http_ok() { return 1; }
  verify_active_release_health "$FULL_TARGET" "$FULL_RUNTIME"
) >/dev/null 2>&1; then
  fail "equal-target dashboard health failure was accepted"
fi
rm "$CURRENT_LINK"

PARTIAL_TARGET=cccccccccccccccccccccccccccccccccccccccc
PARTIAL_RUNTIME="$RELEASES_DIR/v-partial-activation"
mkdir -p "$PARTIAL_RUNTIME"
write_release_receipt noop "$PARTIAL_TARGET" "$PARTIAL_TARGET" "$PARTIAL_RUNTIME" \
  "$SOURCE_HASH" "$SOURCE_HASH" "partial equal-target fixture" >/dev/null
if find_full_activation_receipt "$PARTIAL_TARGET" "$PARTIAL_RUNTIME" >/dev/null; then
  fail "a no-op receipt falsely certified a partial activation"
fi

# Governed refresh deployment stages the old exact bytes, atomically installs
# the release source, and can restore bootstrap-era bytes when the old release
# predates vendoring.
GOVERNED_RELEASE="$RELEASES_DIR/v1.0.0-governed"
mkdir -p "$GOVERNED_RELEASE/$(dirname "$VENDORED_REFRESH_REL")"
printf '#!/usr/bin/env python3\nprint("new")\n' > "$GOVERNED_RELEASE/$VENDORED_REFRESH_REL"
stage_refresh_backup
old_refresh_hash="$(sha256_file "$REFRESH_BACKUP_FILE")"
install_governed_refresh "$GOVERNED_RELEASE" >/dev/null
[ "$(sha256_file "$DEPLOYED_REFRESH")" = "$(sha256_file "$GOVERNED_RELEASE/$VENDORED_REFRESH_REL")" ] \
  || fail "governed refresh install did not match release source"
EMPTY_OLD_RELEASE="$RELEASES_DIR/v0.9.0-pre-vendor"
mkdir "$EMPTY_OLD_RELEASE"
restore_governed_refresh_for_release "$EMPTY_OLD_RELEASE" >/dev/null
[ "$(sha256_file "$DEPLOYED_REFRESH")" = "$old_refresh_hash" ] \
  || fail "governed refresh rollback did not restore staged pre-vendor bytes"

# Real cuts retain filesystem validation: tree metadata is a dry-run-only
# substitute and must never make missing or symlinked sources executable.
MISSING_GOVERNED_RELEASE="$RELEASES_DIR/v1.0.1-missing-governed"
mkdir "$MISSING_GOVERNED_RELEASE"
if install_governed_refresh "$MISSING_GOVERNED_RELEASE"; then
  fail "real cut accepted missing governed refresh source"
fi
SYMLINK_GOVERNED_RELEASE="$RELEASES_DIR/v1.0.2-symlink-governed"
mkdir -p "$SYMLINK_GOVERNED_RELEASE/$(dirname "$VENDORED_REFRESH_REL")"
ln -s "$DEPLOYED_REFRESH" "$SYMLINK_GOVERNED_RELEASE/$VENDORED_REFRESH_REL"
if install_governed_refresh "$SYMLINK_GOVERNED_RELEASE"; then
  fail "real cut accepted symlinked governed refresh source"
fi

# The release path invokes the release-owned launchd reconciler with the exact
# source root and records that rollback is now required. Rollback uses the
# deployed reconciler through runtime-current and asks it to reload both jobs.
LAUNCHD_RELEASE="$RELEASES_DIR/v1.1.0-launchd"
LAUNCHD_RECONCILER="$LAUNCHD_RELEASE/$VENDORED_LAUNCHD_RECONCILER_REL"
mkdir -p "$(dirname "$LAUNCHD_RECONCILER")" "$LAUNCHD_RELEASE/venv/bin"
printf '# placeholder reconciler\n' > "$LAUNCHD_RECONCILER"
cat > "$LAUNCHD_RELEASE/venv/bin/python" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$HERMES_HOME/launchd-reconciler-calls"
case " $* " in
  *" install "*)
    [ "${FAKE_RECONCILER_FAIL_INSTALL:-0}" -eq 0 ] || exit 42
    source_root=
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--source-root" ]; then
        source_root="$2"
        break
      fi
      shift
    done
    [ -n "$source_root" ] || exit 43
    mkdir -p "$HERMES_HOME/scripts"
    cp "$source_root/reconcile_launchd_environment.py" \
      "$HERMES_HOME/scripts/reconcile_launchd_environment.py"
    chmod 0755 "$HERMES_HOME/scripts/reconcile_launchd_environment.py"
    ;;
esac
SH
chmod 0755 "$LAUNCHD_RELEASE/venv/bin/python"
install_governed_launchd_environment "$LAUNCHD_RELEASE"
[ "$LAUNCHD_ENV_CHANGED" -eq 1 ] \
  || fail "launchd install did not arm release rollback"
grep -Fq "install --source-root $(dirname "$LAUNCHD_RECONCILER")" \
  "$HERMES_HOME/launchd-reconciler-calls" \
  || fail "release did not invoke launchd install from its canonical source root"
cmp -s "$LAUNCHD_RECONCILER" \
  "$HERMES_HOME/scripts/reconcile_launchd_environment.py" \
  || fail "launchd install did not deploy its rollback reconciler"
# Stage a minimal working fleet-config bundle (installer + manifest +
# skills-policy, plus a fake venv/bin/python that exits 0) so
# install_governed_fleet_config succeeds for real against a release fixture
# that isn't specifically exercising its missing/symlink guards. Mirrors the
# placeholder-reconciler + fake-venv-python convention used for the launchd
# and marketplace reconcilers above.
stage_fleet_config_bundle() {
  local release_dir="${1:?release dir required}"
  mkdir -p "$release_dir/$(dirname "$VENDORED_FLEET_CONFIG_INSTALLER_REL")" \
    "$release_dir/venv/bin"
  printf '# placeholder fleet-config installer\n' \
    > "$release_dir/$VENDORED_FLEET_CONFIG_INSTALLER_REL"
  printf '{}\n' > "$release_dir/$VENDORED_FLEET_CONFIG_MANIFEST_REL"
  printf '{}\n' > "$release_dir/$VENDORED_FLEET_CONFIG_SKILLS_POLICY_REL"
  cat > "$release_dir/venv/bin/python" <<'SH'
#!/usr/bin/env bash
exit 0
SH
  chmod 0755 "$release_dir/venv/bin/python"
}

MISSING_RECONCILER_RELEASE="$RELEASES_DIR/v1.1.1-missing-reconciler"
mkdir "$MISSING_RECONCILER_RELEASE"
if install_governed_launchd_environment "$MISSING_RECONCILER_RELEASE"; then
  fail "real cut accepted missing launchd reconciler"
fi
if install_governed_marketplace_skills "$MISSING_RECONCILER_RELEASE"; then
  fail "real cut accepted missing marketplace reconciler"
fi
SYMLINK_RECONCILER_RELEASE="$RELEASES_DIR/v1.1.2-symlink-reconciler"
mkdir -p "$SYMLINK_RECONCILER_RELEASE/$(dirname "$VENDORED_LAUNCHD_RECONCILER_REL")"
ln -s "$LAUNCHD_RECONCILER" \
  "$SYMLINK_RECONCILER_RELEASE/$VENDORED_LAUNCHD_RECONCILER_REL"
ln -s "$LAUNCHD_RECONCILER" \
  "$SYMLINK_RECONCILER_RELEASE/$VENDORED_SKILLS_RECONCILER_REL"
if install_governed_launchd_environment "$SYMLINK_RECONCILER_RELEASE"; then
  fail "real cut accepted symlinked launchd reconciler"
fi
if install_governed_marketplace_skills "$SYMLINK_RECONCILER_RELEASE"; then
  fail "real cut accepted symlinked marketplace reconciler"
fi

# The fleet-config bundle (installer + manifest + skills-policy) gets the same
# missing/symlink fail-closed treatment as the other governed reconcilers.
if install_governed_fleet_config "$MISSING_RECONCILER_RELEASE"; then
  fail "real cut accepted missing fleet-config installer"
fi
FLEET_CONFIG_MISSING_MANIFEST_RELEASE="$RELEASES_DIR/v1.1.3-missing-fleet-config-manifest"
mkdir -p "$FLEET_CONFIG_MISSING_MANIFEST_RELEASE/$(dirname "$VENDORED_FLEET_CONFIG_INSTALLER_REL")"
printf '# placeholder fleet-config installer\n' \
  > "$FLEET_CONFIG_MISSING_MANIFEST_RELEASE/$VENDORED_FLEET_CONFIG_INSTALLER_REL"
if install_governed_fleet_config "$FLEET_CONFIG_MISSING_MANIFEST_RELEASE"; then
  fail "real cut accepted fleet-config bundle missing its manifest/skills-policy"
fi
SYMLINK_FLEET_CONFIG_RELEASE="$RELEASES_DIR/v1.1.4-symlink-fleet-config"
stage_fleet_config_bundle "$SYMLINK_FLEET_CONFIG_RELEASE"
ln -sf "$LAUNCHD_RECONCILER" \
  "$SYMLINK_FLEET_CONFIG_RELEASE/$VENDORED_FLEET_CONFIG_INSTALLER_REL"
if install_governed_fleet_config "$SYMLINK_FLEET_CONFIG_RELEASE"; then
  fail "real cut accepted symlinked fleet-config installer"
fi

# Regression: install_governed_fleet_config must invoke the fleet-config
# installer DIRECTLY, never via guarded_or_direct/guarded_production_write.
# The installer is itself a production-write-lease actor (acquires its own
# fenced lease + mutation_guard); nesting it inside the cut's own lease guard
# self-deadlocks the single-writer production-write-lease.db. Stub both
# guard entry points to fail loudly if the installer is routed through
# either of them, then prove a real staged bundle still installs cleanly.
FLEET_CONFIG_DIRECT_RELEASE="$RELEASES_DIR/v1.1.5-direct-fleet-config"
stage_fleet_config_bundle "$FLEET_CONFIG_DIRECT_RELEASE"
if (
  # shellcheck disable=SC2329 # must not be invoked by install_governed_fleet_config.
  guarded_or_direct() {
    fail "install_governed_fleet_config routed the installer through guarded_or_direct (self-deadlock regression)"
  }
  # shellcheck disable=SC2329 # must not be invoked by install_governed_fleet_config.
  guarded_production_write() {
    fail "install_governed_fleet_config routed the installer through guarded_production_write (self-deadlock regression)"
  }
  install_governed_fleet_config "$FLEET_CONFIG_DIRECT_RELEASE"
); then
  :
else
  fail "install_governed_fleet_config failed when invoking its installer directly"
fi

# Fleet-outcome sources may resolve from the repository root rather than the
# mini-scripts bundle. Exercise the real install function and pin the complete
# load-bearing argument pair: --repo-root must equal this release directory.
FLEET_OUTCOMES_RELEASE="$RELEASES_DIR/v1.1.6-fleet-outcomes-repo-root"
FLEET_OUTCOMES_RECONCILER="$FLEET_OUTCOMES_RELEASE/$VENDORED_FLEET_OUTCOMES_RECONCILER_REL"
FLEET_OUTCOMES_MANIFEST="$FLEET_OUTCOMES_RELEASE/$VENDORED_FLEET_OUTCOMES_MANIFEST_REL"
FLEET_OUTCOMES_CALLS="$TEST_ROOT/fleet-outcomes-reconciler-calls"
mkdir -p "$(dirname "$FLEET_OUTCOMES_RECONCILER")" "$FLEET_OUTCOMES_RELEASE/venv/bin"
printf '# placeholder fleet-outcomes reconciler\n' > "$FLEET_OUTCOMES_RECONCILER"
printf '{}\n' > "$FLEET_OUTCOMES_MANIFEST"
cat > "$FLEET_OUTCOMES_RELEASE/venv/bin/python" <<SH
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$FLEET_OUTCOMES_CALLS"
SH
chmod 0755 "$FLEET_OUTCOMES_RELEASE/venv/bin/python"
if (
  guarded_or_direct() { "$@"; }
  install_governed_fleet_outcomes "$FLEET_OUTCOMES_RELEASE"
); then
  :
else
  fail "install_governed_fleet_outcomes failed for a valid release bundle"
fi
grep -Fqx "$FLEET_OUTCOMES_RECONCILER install --source-root $(dirname "$FLEET_OUTCOMES_RECONCILER") --repo-root $FLEET_OUTCOMES_RELEASE --manifest $FLEET_OUTCOMES_MANIFEST --home $HOME --hermes-home $HERMES_HOME --reload" \
  "$FLEET_OUTCOMES_CALLS" \
  || fail "fleet-outcome install did not pass --repo-root equal to the release directory"

# A later cut starts with an unarmed in-process marker. If reconciliation fails
# during validation/before snapshot creation, it must not consume the previous
# successful generation's rollback pointer.
LAUNCHD_ENV_CHANGED=0
calls_before_failed_install="$(wc -l < "$HERMES_HOME/launchd-reconciler-calls" | tr -d ' ')"
export FAKE_RECONCILER_FAIL_INSTALL=1
if install_governed_launchd_environment "$LAUNCHD_RELEASE"; then
  fail "failed launchd validation/install was reported as successful"
fi
unset FAKE_RECONCILER_FAIL_INSTALL
[ "$LAUNCHD_ENV_CHANGED" -eq 0 ] \
  || fail "failed pre-snapshot launchd install incorrectly armed rollback"
rollback_governed_launchd_environment "failed pre-snapshot validation"
calls_after_failed_rollback="$(wc -l < "$HERMES_HOME/launchd-reconciler-calls" | tr -d ' ')"
[ "$calls_after_failed_rollback" -eq $((calls_before_failed_install + 1)) ] \
  || fail "failed pre-snapshot install rolled back a prior successful generation"

# The first successful generation remains independently rollback-capable.
LAUNCHD_ENV_CHANGED=1
ln -s "$LAUNCHD_RELEASE" "$CURRENT_LINK"
rollback_governed_launchd_environment "test rollback"
[ "$LAUNCHD_ENV_CHANGED" -eq 0 ] \
  || fail "launchd rollback did not clear the changed marker"
grep -Fq "reconcile_launchd_environment.py rollback" \
  "$HERMES_HOME/launchd-reconciler-calls" \
  || fail "release rollback did not invoke the deployed launchd snapshot restore"
rm "$CURRENT_LINK"

# Marketplace-skill reconciliation is a separately armed release transaction:
# failed validation cannot consume an older snapshot, while a successful
# install is restored through the deployed reconciler during rollback.
SKILLS_RECONCILER="$LAUNCHD_RELEASE/$VENDORED_SKILLS_RECONCILER_REL"
printf '# placeholder skills reconciler\n' > "$SKILLS_RECONCILER"
install_governed_marketplace_skills "$LAUNCHD_RELEASE"
[ "$MARKETPLACE_SKILLS_CHANGED" -eq 1 ] \
  || fail "marketplace skills install did not arm release rollback"
grep -Fq "reconcile_marketplace_skills.py install --source-root $(dirname "$SKILLS_RECONCILER")" \
  "$HERMES_HOME/launchd-reconciler-calls" \
  || fail "release did not invoke marketplace skills install from canonical source root"

MARKETPLACE_SKILLS_CHANGED=0
export FAKE_RECONCILER_FAIL_INSTALL=1
if install_governed_marketplace_skills "$LAUNCHD_RELEASE"; then
  fail "failed marketplace skills validation/install was reported as successful"
fi
unset FAKE_RECONCILER_FAIL_INSTALL
[ "$MARKETPLACE_SKILLS_CHANGED" -eq 0 ] \
  || fail "failed marketplace skills install incorrectly armed rollback"

MARKETPLACE_SKILLS_CHANGED=1
cp "$SKILLS_RECONCILER" "$HERMES_HOME/scripts/reconcile_marketplace_skills.py"
ln -s "$LAUNCHD_RELEASE" "$CURRENT_LINK"
rollback_governed_marketplace_skills "test rollback"
[ "$MARKETPLACE_SKILLS_CHANGED" -eq 0 ] \
  || fail "marketplace skills rollback did not clear the changed marker"
grep -Fq "reconcile_marketplace_skills.py rollback" \
  "$HERMES_HOME/launchd-reconciler-calls" \
  || fail "release rollback did not invoke marketplace skills snapshot restore"
rm "$CURRENT_LINK"

# Release cuts run over SSH, but each service may already be registered in a
# different launchd domain.  Resolve labels independently, preferring gui,
# then user, and only consult managername for an unloaded label.
DOMAIN_PROBE_LOG="$TEST_ROOT/launchd-domain-probes.log"
(
  : > "$DOMAIN_PROBE_LOG"
  # shellcheck disable=SC2329 # invoked indirectly by launchd_target.
  launchctl() {
    printf '%s\n' "$*" >> "$DOMAIN_PROBE_LOG"
    case "${1:-}:${2:-}" in
      print:"$GUI_DOMAIN/$GATEWAY_LABEL") return 1 ;;
      print:"$USER_DOMAIN/$GATEWAY_LABEL") return 0 ;;
      print:"$GUI_DOMAIN/$DASHBOARD_LABEL") return 0 ;;
      managername:*) printf 'Background\n'; return 0 ;;
      *) return 1 ;;
    esac
  }
  [ "$(launchd_target "$GATEWAY_LABEL")" = "$USER_DOMAIN/$GATEWAY_LABEL" ] \
    || fail 'gateway did not resolve its existing user-domain registration'
  [ "$(launchd_target "$DASHBOARD_LABEL")" = "$GUI_DOMAIN/$DASHBOARD_LABEL" ] \
    || fail 'dashboard did not resolve its existing gui-domain registration'
  ! grep -q '^managername' "$DOMAIN_PROBE_LOG" \
    || fail 'managername was consulted for a loaded service'
  gateway_probes="$(grep "$GATEWAY_LABEL" "$DOMAIN_PROBE_LOG")"
  [ "$gateway_probes" = "print $GUI_DOMAIN/$GATEWAY_LABEL
print $USER_DOMAIN/$GATEWAY_LABEL" ] \
    || fail 'launchd domain resolver did not probe gui before user'
)

for manager_fixture in Aqua Background; do
  (
    # shellcheck disable=SC2329 # invoked indirectly by resolve_launchd_domain.
    launchctl() {
      case "${1:-}" in
        print) return 1 ;;
        managername) printf '%s\n' "$manager_fixture"; return 0 ;;
        *) return 1 ;;
      esac
    }
    expected_domain="$USER_DOMAIN"
    [ "$manager_fixture" = Aqua ] && expected_domain="$GUI_DOMAIN"
    [ "$(resolve_launchd_domain "$GATEWAY_LABEL")" = "$expected_domain" ] \
      || fail "managername $manager_fixture selected the wrong unloaded-service domain"
  )
done

# Keep the remaining rollback fixtures independent of the host's launchd
# state; the resolver behavior itself is covered above.
resolve_launchd_domain() { printf '%s\n' "$GUI_DOMAIN"; }

# A rollback whose gateway is healthy but dashboard remains unhealthy must
# terminate nonzero; a warning-only rollback would make this subshell succeed.
PREVIOUS_RELEASE="$RELEASES_DIR/v1.2.3-123456789abc"
FAILED_ROLLBACK_SOURCE="$RELEASES_DIR/v1.2.2-deadbeefdead"
mkdir -p "$FAILED_ROLLBACK_SOURCE"
mkdir -p "$PREVIOUS_RELEASE/scripts" "$PREVIOUS_RELEASE/$(dirname "$VENDORED_REFRESH_REL")"
printf '#!/usr/bin/env bash\nprintf previous\n' > "$PREVIOUS_RELEASE/scripts/cu-clickup"
chmod 0755 "$PREVIOUS_RELEASE/scripts/cu-clickup"
printf '#!/usr/bin/env python3\nprint("previous")\n' > "$PREVIOUS_RELEASE/$VENDORED_REFRESH_REL"
stage_fleet_config_bundle "$PREVIOUS_RELEASE"
printf '%s\n' "$PREVIOUS_RELEASE" > "$PREV_FILE"
ln -s "$FAILED_ROLLBACK_SOURCE" "$CURRENT_LINK"
ROLLBACK_KICKSTART_LOG="$TEST_ROOT/rollback-kickstarts.log"
: > "$TEST_ROOT/no-rollback-receipt"
rm "$TEST_ROOT/no-rollback-receipt"
: > "$ROLLBACK_KICKSTART_LOG"
if (
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  repoint_symlink() { :; }
  # Prove rollback resolves each label at the point of restart instead of
  # reusing one session-derived domain for both services.
  # shellcheck disable=SC2329 # invoked indirectly by launchd_target.
  resolve_launchd_domain() {
    [ "${1:-}" = "$GATEWAY_LABEL" ] && printf '%s\n' "$USER_DOMAIN" \
      || printf '%s\n' "$GUI_DOMAIN"
  }
  # shellcheck disable=SC2329 # invoked indirectly by guarded_kickstart_label.
  guarded_production_write() {
    [ "$1" = launchctl ] && [ "$2" = kickstart ] && [ "$3" = -k ] || return 1
    printf '%s\n' "$4" >> "$ROLLBACK_KICKSTART_LOG"
  }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_gateway() { return 0; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_dashboard() { return 1; }
  record_rollback_receipt() { : > "$TEST_ROOT/no-rollback-receipt"; }
  rollback_to_previous 'safety harness'
) >/dev/null 2>&1; then
  fail 'rollback returned success despite dashboard health failure'
fi
[ "$(cat "$ROLLBACK_KICKSTART_LOG")" = "$USER_DOMAIN/$GATEWAY_LABEL
$GUI_DOMAIN/$DASHBOARD_LABEL" ] \
  || fail 'rollback did not resolve gateway and dashboard kickstarts independently'
[ ! -e "$TEST_ROOT/no-rollback-receipt" ] \
  || fail 'failed rollback wrote a success receipt'

# A successor takeover at the exact dashboard kickstart guard must prevent
# launchctl, generation rotation, and receipt writes after the gateway restart.
rm "$CURRENT_LINK"
ln -s "$FAILED_ROLLBACK_SOURCE" "$CURRENT_LINK"
printf '%s\n' "$PREVIOUS_RELEASE" > "$PREV_FILE"
rm -f "$TEST_ROOT/dashboard-after-takeover" "$TEST_ROOT/rotate-after-takeover" \
  "$TEST_ROOT/receipt-after-takeover"
if (
  heartbeat_production_write_lease() { :; }
  install_clickup_cli() { :; }
  rollback_governed_fleet_outcomes() { :; }
  restore_governed_pr_pipeline_for_release() { :; }
  rollback_governed_marketplace_skills() { :; }
  rollback_governed_launchd_environment() { :; }
  verify_gateway() { return 0; }
  launchctl() {
    [ "${3:-}" = "$GUI_DOMAIN/$DASHBOARD_LABEL" ] \
      && : > "$TEST_ROOT/dashboard-after-takeover"
    return 0
  }
  guard_count=0
  guarded_production_write() {
    guard_count=$((guard_count + 1))
    [ "$guard_count" -eq 1 ] || return 77
    "$@"
  }
  write_previous_target() { : > "$TEST_ROOT/rotate-after-takeover"; }
  record_rollback_receipt() { : > "$TEST_ROOT/receipt-after-takeover"; }
  rollback_to_previous 'dashboard guard takeover'
) >/dev/null 2>&1; then
  fail 'rollback succeeded after successor takeover at dashboard kickstart guard'
fi
[ ! -e "$TEST_ROOT/dashboard-after-takeover" ] \
  || fail 'dashboard kickstart executed after successor takeover'
[ ! -e "$TEST_ROOT/rotate-after-takeover" ] \
  || fail 'rollback generation rotated after successor takeover'
[ ! -e "$TEST_ROOT/receipt-after-takeover" ] \
  || fail 'rollback receipt was written after successor takeover'

# A fully verified explicit rollback records an immutable rollback receipt and
# rotates .previous to the former active release, making the operator action
# reversible. Neither action occurs before both service verifiers pass.
rm "$CURRENT_LINK"
ln -s "$FAILED_ROLLBACK_SOURCE" "$CURRENT_LINK"
printf '%s\n' "$PREVIOUS_RELEASE" > "$PREV_FILE"
(
  heartbeat_production_write_lease() { :; }
  guarded_kickstart_label() { :; }
  verify_gateway() { return 0; }
  verify_dashboard() { return 0; }
  install_clickup_cli() { return 0; }
  rollback_governed_fleet_outcomes() { return 0; }
  restore_governed_pr_pipeline_for_release() { return 0; }
  rollback_governed_marketplace_skills() { return 0; }
  rollback_governed_launchd_environment() { return 0; }
  release_commit_sha() {
    if [ "$1" = "$FAILED_ROLLBACK_SOURCE" ]; then
      printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    else
      printf '%s\n' bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    fi
  }
  PR_PIPELINE_RECEIPT_ID=3333333333333333333333333333333333333333333333333333333333333333
  REVIEW_GATE_SMOKE_STATUS=passed
  rollback_to_previous 'explicit --rollback fixture'
) >/dev/null
[ "$(readlink "$CURRENT_LINK")" = "$PREVIOUS_RELEASE" ] \
  || fail 'verified explicit rollback did not activate the previous release'
[ "$(cat "$PREV_FILE")" = "$FAILED_ROLLBACK_SOURCE" ] \
  || fail 'verified explicit rollback did not rotate .previous to the former active release'
ROLLBACK_RECEIPT_ID="$(sha256_file "$LAST_RECEIPT_FILE")"
[ -f "$RELEASES_DIR/.mini-release-receipt-$ROLLBACK_RECEIPT_ID.json" ] \
  || fail 'verified rollback receipt is not preserved at its content address'
python3 - "$LAST_RECEIPT_FILE" "$FAILED_ROLLBACK_SOURCE" "$PREVIOUS_RELEASE" <<'PY' \
  || fail 'verified rollback receipt is not truthful'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["event"] == "rollback"
assert payload["from_commit"] == "a" * 40
assert payload["to_commit"] == "b" * 40
assert payload["runtime_target"] == sys.argv[3]
assert payload["certified_source_commit"] is None
assert payload["promotion_authority_receipt_id"] is None
assert payload["detail"] == "verified rollback: explicit --rollback fixture"
PY
printf '%s\n' "$PREVIOUS_RELEASE" > "$PREV_FILE"

# Either service can fail to kickstart immediately after runtime-current is
# switched. Both failures must restore the recorded previous target and still
# report a failed cut after rollback succeeds.
NEW_RELEASE="$RELEASES_DIR/v1.2.4-abcdef123456"
mkdir "$NEW_RELEASE"
for failed_service in gateway dashboard; do
  printf '%s\n' "$PREVIOUS_RELEASE" > "$PREV_FILE"
  repoint_symlink "$NEW_RELEASE" >/dev/null
  failed_target="$GUI_DOMAIN/$GATEWAY_LABEL"
  [ "$failed_service" = dashboard ] && failed_target="$GUI_DOMAIN/$DASHBOARD_LABEL"
  if (
    kickstart_calls=0
    # Fail only the post-switch restart; rollback restarts then succeed.
    # shellcheck disable=SC2329 # invoked by kickstart_after_switch/rollback_to_previous.
    kickstart() {
      kickstart_calls=$((kickstart_calls + 1))
      [ "$kickstart_calls" -ne 1 ]
    }
    guarded_kickstart_label() { :; }
    # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
    verify_gateway() { return 0; }
    # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
    verify_dashboard() { return 0; }
    record_rollback_receipt() { return 0; }
    kickstart_after_switch "$failed_target" "$failed_service"
  ) >/dev/null 2>&1; then
    fail "$failed_service kickstart failure returned success after rollback"
  fi
  [ "$(readlink "$CURRENT_LINK")" = "$PREVIOUS_RELEASE" ] \
    || fail "$failed_service kickstart failure left runtime-current on the new release"
  [ "$(cat "$PREV_FILE")" = "$NEW_RELEASE" ] \
    || fail "$failed_service automatic rollback did not preserve the failed generation"
done

# Receipt path validation failures occur after the runtime and governed script
# have switched. They must return through the caller's rollback branch rather
# than exiting from a nested path assertion and leaving the new release live.
printf '%s\n' "$PREVIOUS_RELEASE" > "$PREV_FILE"
repoint_symlink "$NEW_RELEASE" >/dev/null
printf '#!/usr/bin/env python3\nprint("new live")\n' > "$DEPLOYED_REFRESH"
RECEIPT_OUTSIDE="$TEST_ROOT/receipt-outside"
printf 'must remain untouched\n' > "$RECEIPT_OUTSIDE"
rm -f "$LAST_RECEIPT_FILE"
ln -s "$RECEIPT_OUTSIDE" "$LAST_RECEIPT_FILE"
if (
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  guarded_kickstart_label() { :; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_gateway() { return 0; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_dashboard() { return 0; }
  # Keep this regression focused on runtime/refresh restoration.
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  install_clickup_cli() { :; }
  release_commit_sha() {
    [ "$1" = "$NEW_RELEASE" ] && printf '%040d\n' 1 || printf '%040d\n' 2
  }
  record_cut_receipt_or_rollback advanced \
    aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
    "$NEW_RELEASE" "$SOURCE_HASH" "$DEPLOYED_HASH" \
    "post-switch receipt validation regression"
) >/dev/null 2>&1; then
  fail "receipt symlink failure returned success after rollback"
fi
[ "$(readlink "$CURRENT_LINK")" = "$PREVIOUS_RELEASE" ] \
  || fail "receipt symlink failure left runtime-current on the new release"
[ "$(sha256_file "$DEPLOYED_REFRESH")" = "$(sha256_file "$PREVIOUS_RELEASE/$VENDORED_REFRESH_REL")" ] \
  || fail "receipt symlink failure did not restore previous governed refresh bytes"
[ "$(cat "$RECEIPT_OUTSIDE")" = "must remain untouched" ] \
  || fail "receipt symlink failure modified the symlink target"
rm "$LAST_RECEIPT_FILE"

# A reconciler can fail after its first managed write. Rollback must already be
# armed before that subprocess begins, not only after its JSON report parses.
PARTIAL_PR_RELEASE="$RELEASES_DIR/v1.2.5-partial-pr"
mkdir -p "$PARTIAL_PR_RELEASE/$(dirname "$VENDORED_PR_PIPELINE_RECONCILER_REL")" \
  "$PARTIAL_PR_RELEASE/machine-setup/mini-scripts/pr_pipeline" \
  "$PARTIAL_PR_RELEASE/venv/bin"
printf '# placeholder\n' > "$PARTIAL_PR_RELEASE/$VENDORED_PR_PIPELINE_RECONCILER_REL"
printf '{}\n' > "$PARTIAL_PR_RELEASE/machine-setup/mini-scripts/pr_pipeline/manifest.json"
cat > "$PARTIAL_PR_RELEASE/venv/bin/python" <<'SH'
#!/usr/bin/env bash
printf 'partial\n' > "$HERMES_HOME/partial-pr-pipeline-write"
exit 42
SH
chmod 0755 "$PARTIAL_PR_RELEASE/venv/bin/python"
PR_PIPELINE_CHANGED=0
if reconcile_governed_pr_pipeline \
  "$PARTIAL_PR_RELEASE" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; then
  fail "partially failed PR-pipeline reconciliation reported success"
fi
[ "$PR_PIPELINE_CHANGED" -eq 1 ] \
  || fail "partial PR-pipeline failure did not arm rollback"
[ -f "$HERMES_HOME/partial-pr-pipeline-write" ] \
  || fail "partial PR-pipeline fixture did not reach its first write"
PR_PIPELINE_CHANGED=0

# If governed freeze persistence fails, the prior stable authorization pointer
# is removed so a subsequent poll cannot reuse the old unfrozen SHA.
FREEZE_RELEASE="$RELEASES_DIR/v1.2.6-freeze-failure"
mkdir -p "$FREEZE_RELEASE/scripts" "$FREEZE_RELEASE/venv/bin"
printf '# placeholder\n' > "$FREEZE_RELEASE/scripts/mini-release-poll-control.py"
cat > "$FREEZE_RELEASE/venv/bin/python" <<'SH'
#!/usr/bin/env bash
exit 42
SH
chmod 0755 "$FREEZE_RELEASE/venv/bin/python"
repoint_symlink "$FREEZE_RELEASE" >/dev/null
printf 'old authorization\n' > "$RELEASES_DIR/.mini-release-poll-control.json"
rm -f "$RELEASES_DIR/.mini-release-poll-control.invalidated"
IF_ADVANCED=1
PREFLIGHT=0
DRY_RUN=0
if freeze_managed_poll_after_failure; then
  fail "failed governed freeze was reported as successful"
fi
[ ! -e "$RELEASES_DIR/.mini-release-poll-control.json" ] \
  || fail "freeze failure left prior authorization active"
[ -f "$RELEASES_DIR/.mini-release-poll-control.invalidated" ] \
  || fail "freeze failure did not preserve invalidation evidence"
repoint_symlink "$PREVIOUS_RELEASE" >/dev/null
IF_ADVANCED=0

# End-to-end dry cuts validate governed sources from the immutable target tree,
# never create the planned release directory, and leave the active runtime,
# deployed refresh, receipts, reloads, and reconcilers untouched. The target
# refresh bytes are deliberately different from the deployed bytes: dry-run
# must plan post-install verification, not compare against undeployed state.
DRY_ROOT="$(cd -P "$TEST_ROOT" && pwd -P)/dry-run"
DRY_HOME="$DRY_ROOT/home"
DRY_HERMES="$DRY_HOME/.hermes"
DRY_RELEASES="$DRY_HERMES/releases"
DRY_ACTIVE="$DRY_RELEASES/v1.9.0-active"
DRY_BIN="$DRY_ROOT/bin"
DRY_GIT_LOG="$DRY_ROOT/git.log"
DRY_RELOAD_LOG="$DRY_ROOT/reloads.log"
DRY_TARGET_SHA=cccccccccccccccccccccccccccccccccccccccc
DRY_TARGET_BLOB=dddddddddddddddddddddddddddddddddddddddd
mkdir -p "$DRY_ACTIVE" "$DRY_HERMES/scripts" "$DRY_BIN"
ln -s "$DRY_ACTIVE" "$DRY_HERMES/runtime-current"
printf '#!/usr/bin/env python3\nprint("deployed refresh")\n' > "$DRY_HERMES/scripts/clickup_workspace_refresh.py"
chmod 0755 "$DRY_HERMES/scripts/clickup_workspace_refresh.py"
dry_refresh_before="$(sha256_file "$DRY_HERMES/scripts/clickup_workspace_refresh.py")"
dry_target_refresh_hash="$(printf '#!/usr/bin/env python3\nprint("target refresh")\n' | shasum -a 256 | awk '{print $1}')"
[ "$dry_refresh_before" != "$dry_target_refresh_hash" ] \
  || fail "dry-run fixture did not use divergent deployed and target refresh content"
cat > "$DRY_BIN/git" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$DRY_GIT_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -C|-c) shift 2 ;;
    *) break ;;
  esac
done
case "${1:-}" in
  archive)
    # The pre-fetch bootstrap lease intentionally archives the already-active
    # runtime.  The fake remote SHA is synthetic, so source the lease module
    # from this checked-out test repository instead.
    shift 2
    /usr/bin/git -C "$FAKE_REPO" archive HEAD "$@"
    ;;
  remote)
    printf 'ssh://example.invalid/hermes-agent.git\n'
    ;;
  rev-parse)
    target="${!#}"
    case "$target" in
      origin/*) printf '%s\n' "$DRY_TARGET_SHA" ;;
      HEAD*) printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
      *) exit 1 ;;
    esac
    ;;
  diff)
    # The synthetic target has no Mini runtime changes. Change discovery must
    # still execute successfully now that release admission fails closed.
    exit 0
    ;;
  show|cat-file)
    printf 'unexpected target blob read during dry run\n' >&2
    exit 97
    ;;
  ls-tree)
    [ "${2:-}" = "$DRY_TARGET_SHA" ] && [ "${3:-}" = -- ] || exit 2
    target_path="${4:-}"
    case "$target_path" in
      machine-setup/mini-scripts/clickup_workspace_refresh.py|\
      machine-setup/mini-scripts/reconcile_launchd_environment.py|\
      machine-setup/mini-scripts/reconcile_marketplace_skills.py|\
      machine-setup/mini-scripts/reconcile_pr_pipeline.py|\
      machine-setup/mini-scripts/reconcile_fleet_outcomes.py|\
      machine-setup/fleet-config/install_fleet_config.py)
        printf '100755 blob %s\t%s\n' "$DRY_TARGET_BLOB" "$target_path"
        ;;
      machine-setup/mini-scripts/fleet_outcome_manifest.json|\
      machine-setup/fleet-config/fleet_config_manifest.json|\
      machine-setup/fleet-config/skills-policy.json)
        printf '100644 blob %s\t%s\n' "$DRY_TARGET_BLOB" "$target_path"
        ;;
      *) exit 3 ;;
    esac
    ;;
  *) exit 2 ;;
esac
SH
cat > "$DRY_BIN/launchctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DRY_RELOAD_LOG"
SH
cat > "$DRY_BIN/npm" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$DRY_BIN/uv" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod 0755 "$DRY_BIN/git" "$DRY_BIN/launchctl" "$DRY_BIN/npm" "$DRY_BIN/uv"
if ! HOME="$DRY_HOME" HERMES_HOME="$DRY_HERMES" PATH="$DRY_BIN:$PATH" \
  DRY_GIT_LOG="$DRY_GIT_LOG" DRY_RELOAD_LOG="$DRY_RELOAD_LOG" \
  DRY_TARGET_SHA="$DRY_TARGET_SHA" DRY_TARGET_BLOB="$DRY_TARGET_BLOB" \
  "$SCRIPT" --dry-run --ref dry-target > "$DRY_ROOT/output" 2>&1; then
  fail "end-to-end dry cut failed despite valid immutable target metadata"
fi
DRY_PLANNED="$DRY_RELEASES/v0.0.0-dry-run-${DRY_TARGET_SHA:0:12}"
[ ! -e "$DRY_PLANNED" ] && [ ! -L "$DRY_PLANNED" ] \
  || fail "dry cut created the planned release directory"
[ "$(readlink "$DRY_HERMES/runtime-current")" = "$DRY_ACTIVE" ] \
  || fail "dry cut changed runtime-current"
[ "$(sha256_file "$DRY_HERMES/scripts/clickup_workspace_refresh.py")" = "$dry_refresh_before" ] \
  || fail "dry cut changed the deployed refresh script"
[ ! -e "$DRY_RELEASES/.previous" ] \
  || fail "dry cut wrote the previous-release record"
[ -z "$(find "$DRY_RELEASES" -maxdepth 1 -type f -name '.mini-release-*' -print)" ] \
  || fail "dry cut wrote a release receipt or refresh backup"
[ ! -e "$DRY_RELOAD_LOG" ] || fail "dry cut triggered a launchd reload"
[ ! -e "$DRY_HERMES/launchd-reconciler-calls" ] \
  || fail "dry cut executed a governed reconciler"
dry_marketplace_line="$(grep -nF 'reconcile_marketplace_skills.py install --source-root' "$DRY_ROOT/output" \
  | head -n1 | cut -d: -f1 || true)"
dry_launchd_line="$(grep -nF 'reconcile_launchd_environment.py install --source-root' "$DRY_ROOT/output" \
  | head -n1 | cut -d: -f1 || true)"
dry_drain_request_line="$(grep -nF 'request external gateway drain' "$DRY_ROOT/output" \
  | head -n1 | cut -d: -f1 || true)"
dry_runtime_swap_line="$(grep -nF "ln -sfn $DRY_PLANNED" "$DRY_ROOT/output" \
  | head -n1 | cut -d: -f1 || true)"
dry_drain_clear_line="$(grep -nF 'clear external gateway drain' "$DRY_ROOT/output" \
  | head -n1 | cut -d: -f1 || true)"
[ -n "$dry_marketplace_line" ] \
  || fail "dry cut did not plan marketplace skill reconciliation"
[ -n "$dry_launchd_line" ] \
  || fail "dry cut did not plan launchd reconciliation and gateway start"
[ "$dry_marketplace_line" -lt "$dry_launchd_line" ] \
  || fail "dry cut planned gateway start before marketplace skill reconciliation"
[ -n "$dry_drain_request_line" ] && [ -n "$dry_runtime_swap_line" ] && [ -n "$dry_drain_clear_line" ] \
  || fail "dry cut omitted release drain request, runtime switch, or drain cleanup"
[ "$dry_drain_request_line" -lt "$dry_runtime_swap_line" ] \
  || fail "dry cut planned runtime switch before external drain"
[ "$dry_launchd_line" -lt "$dry_drain_clear_line" ] \
  || fail "dry cut planned drain cleanup before new launchd registration"
# Regression guard for the 592f589e90 (#324) first-cut bootstrap deadlock:
# install_governed_fleet_outcomes reconciles cron_updates against an
# ALREADY-EXISTING job in jobs.json (reconcile_fleet_outcomes.py can only
# patch a job, never create one). A release that both adds a cron job in
# fleet-config/jobs.json and pins that same job in
# fleet_outcome_manifest.json's cron_updates deadlocks on its first cut
# unless install_governed_fleet_config runs first. Assert that ordering
# both in the source text and in the planned dry-run execution order.
fleet_config_install_source_line="$(grep -n 'if ! install_governed_fleet_config "\$NEW_DIR"; then' "$SCRIPT" \
  | head -n1 | cut -d: -f1 || true)"
fleet_outcomes_install_source_line="$(grep -n 'if ! install_governed_fleet_outcomes "\$NEW_DIR"; then' "$SCRIPT" \
  | head -n1 | cut -d: -f1 || true)"
[ -n "$fleet_config_install_source_line" ] && [ -n "$fleet_outcomes_install_source_line" ] \
  || fail "could not locate install_governed_fleet_config/install_governed_fleet_outcomes call sites"
[ "$fleet_config_install_source_line" -lt "$fleet_outcomes_install_source_line" ] \
  || fail "install_governed_fleet_outcomes call site precedes install_governed_fleet_config in source (first-cut deadlock regression, see #324)"
dry_fleet_config_line="$(grep -nF 'install_fleet_config.py --manifest' "$DRY_ROOT/output" \
  | head -n1 | cut -d: -f1 || true)"
dry_fleet_outcomes_line="$(grep -nF 'reconcile_fleet_outcomes.py install --source-root' "$DRY_ROOT/output" \
  | head -n1 | cut -d: -f1 || true)"
[ -n "$dry_fleet_config_line" ] && [ -n "$dry_fleet_outcomes_line" ] \
  || fail "dry cut did not plan fleet-config install or fleet-outcome reconciliation"
[ "$dry_fleet_config_line" -lt "$dry_fleet_outcomes_line" ] \
  || fail "dry cut planned fleet-outcome cron reconciliation before fleet-config job creation (first-cut deadlock regression, see #324)"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_REFRESH_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate refresh metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_LAUNCHD_RECONCILER_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate launchd reconciler metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_SKILLS_RECONCILER_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate marketplace reconciler metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_PR_PIPELINE_RECONCILER_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate PR-pipeline reconciler metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_FLEET_OUTCOMES_RECONCILER_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate fleet-outcome reconciler metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_FLEET_OUTCOMES_MANIFEST_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate fleet-outcome manifest metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_FLEET_CONFIG_INSTALLER_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate fleet-config installer metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_FLEET_CONFIG_MANIFEST_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate fleet-config manifest metadata from the target tree"
grep -Fq "ls-tree $DRY_TARGET_SHA -- $VENDORED_FLEET_CONFIG_SKILLS_POLICY_REL" "$DRY_GIT_LOG" \
  || fail "dry cut did not validate fleet-config skills-policy metadata from the target tree"
if grep -Eq ' (show|cat-file) ' "$DRY_GIT_LOG"; then
  fail "dry cut materialized a target blob"
fi
grep -Fq 'verify governed refresh post-install SHA-256 equality (deferred to real cut)' "$DRY_ROOT/output" \
  || fail "dry cut did not print deferred refresh verification"
grep -Fq 'npm ci --include=dev && npm run build --workspace web' "$DRY_ROOT/output" \
  || fail "dry cut did not advertise lockfile-respecting npm ci web build"
if grep -Fq 'npm install && npm run build --workspace web' "$DRY_ROOT/output"; then
  fail "dry cut advertised lockfile-mutating npm install web build"
fi

# Polling artifacts are source-controlled, point only at the conditional
# release mode, and the plist is parseable without requiring launchd.
POLL_WRAPPER="$SCRIPT_DIR/../../scripts/mini-release-poll.sh"
POLL_INSTALLER="$SCRIPT_DIR/../../scripts/install-mini-release-poller.sh"
POLL_PLIST="$SCRIPT_DIR/../../scripts/launchd/com.colingreig.hermes.release-poll.plist"
grep -Fq -- '--if-advanced' "$POLL_WRAPPER" || fail "poll wrapper does not require conditional mode"
grep -Fq -- '--certified-sha' "$POLL_WRAPPER" || fail "poll wrapper does not bind an exact certified SHA"
grep -Fq -- '--promotion-receipt-id' "$POLL_WRAPPER" \
  || fail "poll wrapper does not bind immutable promotion authority"
grep -Fq -- '--preflight' "$POLL_INSTALLER" || fail "poll installer has no fail-closed preflight"
python3 - "$POLL_PLIST" <<'PY' || fail "release poll plist is not parseable"
import plistlib
import sys
from pathlib import Path

payload = plistlib.loads(Path(sys.argv[1]).read_bytes())
assert payload["Label"] == "com.colingreig.hermes.release-poll"
assert payload["StartInterval"] == 900
assert payload["ProgramArguments"][-1].endswith("/runtime-current/scripts/mini-release-poll.sh")
assert not payload.get("RunAtLoad", False)
PY

# A real fetch failure must occur while holding the release lock and must not
# let the EXIT trap create poll-control state before a verified cut exists.
PREFLIGHT_ROOT="$TEST_ROOT/preflight"
PREFLIGHT_HOME="$PREFLIGHT_ROOT/home"
PREFLIGHT_HERMES="$PREFLIGHT_HOME/.hermes"
PREFLIGHT_RELEASE="$PREFLIGHT_HERMES/releases/v1.0.0-active"
PREFLIGHT_BIN="$PREFLIGHT_ROOT/bin"
PREFLIGHT_STATE="$PREFLIGHT_ROOT/origin-state"
PREFLIGHT_MARKER="$PREFLIGHT_ROOT/fetch-held-lock"
PREFLIGHT_LOG="$PREFLIGHT_ROOT/git.log"
mkdir -p "$PREFLIGHT_RELEASE/scripts/launchd" "$PREFLIGHT_RELEASE/venv/bin" "$PREFLIGHT_BIN"
cp "$SCRIPT" "$PREFLIGHT_RELEASE/scripts/mini-release-cut.sh"
cp "$SCRIPT_DIR/../../scripts/mini-release-poll-control.py" \
  "$PREFLIGHT_RELEASE/scripts/mini-release-poll-control.py"
cp "$POLL_PLIST" \
  "$PREFLIGHT_RELEASE/scripts/launchd/com.colingreig.hermes.release-poll.plist"
chmod 0755 "$PREFLIGHT_RELEASE/scripts/mini-release-cut.sh"
ln -s "$(command -v python3)" "$PREFLIGHT_RELEASE/venv/bin/python"
ln -s "$PREFLIGHT_RELEASE" "$PREFLIGHT_HERMES/runtime-current"
printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa > "$PREFLIGHT_STATE"
cat > "$PREFLIGHT_BIN/git" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$FAKE_GIT_LOG"
case " $* " in
  *" fetch --prune origin "*)
    [ -f "$FAKE_RELEASES_DIR/.mini-release-cut.lock" ] || exit 90
    grep -Fq '"actor":"mini-release-cut"' "$FAKE_RELEASES_DIR/.mini-release-cut.lock" || exit 91
    grep -Fq "\"commit_sha\":\"$FAKE_CERTIFIED_SHA\"" \
      "$FAKE_RELEASES_DIR/.mini-release-cut.lock" || exit 92
    printf 'locked\n' > "$FAKE_FETCH_MARKER"
    exit 42
    ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    -C|-c) shift 2 ;;
    *) break ;;
  esac
done
case "${1:-}" in
  archive)
    shift 2
    /usr/bin/git -C "$FAKE_REPO" archive HEAD "$@"
    ;;
  remote)
    printf 'ssh://example.invalid/hermes-agent.git\n'
    ;;
  rev-parse)
    target="${*: -1}"
    case "$target" in
      origin/*) cat "$FAKE_GIT_STATE" ;;
      HEAD*) printf '%s\n' "$FAKE_ACTIVE_SHA" ;;
      *) exit 1 ;;
    esac
    ;;
  merge-base)
    # The fetched remote is divergent from the active commit in both
    # ancestor directions.
    exit 1
    ;;
  *)
    exit 2
    ;;
esac
SH
chmod 0755 "$PREFLIGHT_BIN/git"
if HOME="$PREFLIGHT_HOME" HERMES_HOME="$PREFLIGHT_HERMES" \
  PATH="$PREFLIGHT_BIN:$PATH" \
  FAKE_GIT_LOG="$PREFLIGHT_LOG" \
  FAKE_GIT_STATE="$PREFLIGHT_STATE" \
  FAKE_RELEASES_DIR="$PREFLIGHT_HERMES/releases" \
  FAKE_FETCH_MARKER="$PREFLIGHT_MARKER" \
  FAKE_REPO="$(cd "$SCRIPT_DIR/../.." && pwd -P)" \
  FAKE_ACTIVE_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  FAKE_CERTIFIED_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  FAKE_REMOTE_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  "$POLL_INSTALLER" --install \
    --certified-sha bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
    --promotion-receipt-id cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
    >"$PREFLIGHT_ROOT/cut.out" 2>&1; then
  fail "installer accepted a failed origin fetch"
fi
[ -f "$PREFLIGHT_MARKER" ] \
  || fail "failed fetch did not run while holding the release lock"
[ ! -e "$PREFLIGHT_HERMES/releases/.mini-release-cut.lock" ] \
  || fail "installer preflight left the release lock behind"
[ ! -e "$PREFLIGHT_HOME/Library/LaunchAgents/com.colingreig.hermes.release-poll.plist" ] \
  || fail "installer wrote the LaunchAgent after failed fetch"
[ ! -e "$PREFLIGHT_HERMES/releases/.mini-release-last-receipt.json" ] \
  || fail "installer preflight wrote a release receipt"
[ ! -e "$PREFLIGHT_HERMES/releases/.mini-release-poll-control.json" ] \
  || fail "fetch failure created poll-control state before lease acquisition"

cleanup_body="$(sed -n '/^cleanup_on_exit() {/,/^}$/p' "$SCRIPT")"
# Any ordinary abort after the marker is armed removes it before slower
# partial-release cleanup and before releasing the production-write lease.
DRAIN_EXIT_ROOT="$TEST_ROOT/drain-exit"
mkdir -p "$DRAIN_EXIT_ROOT"
if (
  eval "$cleanup_body"
  DRY_RUN=0
  NEW_DIR=""
  LEASE_CUT_READY=1
  RELEASE_DRAIN_ARMED=1
  production_write_mutation_allowed() { return 0; }
  clear_release_drain() { : > "$DRAIN_EXIT_ROOT/cleared"; RELEASE_DRAIN_ARMED=0; }
  freeze_managed_poll_after_failure() { return 0; }
  release_cut_lock() { return 0; }
  release_production_write_lease() { return 0; }
  cleanup_production_write_lease_bootstrap() { :; }
  warn() { :; }
  false
  cleanup_on_exit
); then
  fail "release-drain abort cleanup unexpectedly returned success"
fi
[ -f "$DRAIN_EXIT_ROOT/cleared" ] || fail "abort cleanup did not clear release drain marker"

# A nonzero cut after a verified automatic rollback must not let EXIT cleanup
# delete the failed generation now recorded as .previous.
ROLLBACK_EXIT_ROOT="$TEST_ROOT/rollback-generation-exit"
ROLLBACK_EXIT_RELEASES="$ROLLBACK_EXIT_ROOT/releases"
ROLLBACK_EXIT_NEW="$ROLLBACK_EXIT_RELEASES/v-failed-generation"
ROLLBACK_EXIT_LIVE="$ROLLBACK_EXIT_RELEASES/v-restored-generation"
mkdir -p "$ROLLBACK_EXIT_NEW" "$ROLLBACK_EXIT_LIVE"
printf 'preserve failed generation\n' > "$ROLLBACK_EXIT_NEW/sentinel"
printf '%s\n' "$ROLLBACK_EXIT_NEW" > "$ROLLBACK_EXIT_RELEASES/.previous"
ln -s "$ROLLBACK_EXIT_LIVE" "$ROLLBACK_EXIT_ROOT/runtime-current"
if (
  eval "$cleanup_body"
  DRY_RUN=0
  NEW_DIR="$ROLLBACK_EXIT_NEW"
  LEASE_CUT_READY=1
  RELEASES_DIR="$ROLLBACK_EXIT_RELEASES"
  PREV_FILE="$ROLLBACK_EXIT_RELEASES/.previous"
  CURRENT_LINK="$ROLLBACK_EXIT_ROOT/runtime-current"
  production_write_mutation_allowed() { return 0; }
  guarded_production_write() { : > "$ROLLBACK_EXIT_ROOT/delete-attempted"; return 1; }
  freeze_managed_poll_after_failure() { return 0; }
  release_production_write_lease() { :; }
  cleanup_production_write_lease_bootstrap() { :; }
  release_cut_lock() { :; }
  warn() { :; }
  false
  cleanup_on_exit
); then
  fail "rollback-generation cleanup unexpectedly returned success"
fi
[ -f "$ROLLBACK_EXIT_NEW/sentinel" ] \
  || fail "EXIT cleanup deleted the automatic rollback generation"
[ ! -e "$ROLLBACK_EXIT_ROOT/delete-attempted" ] \
  || fail "EXIT cleanup attempted to delete the preserved rollback generation"

# Exercise the actual EXIT cleanup function after a fence-loss result.  Both
# a partially-built NEW_DIR and an already-existing poll authorization must
# survive: the successor owns recovery and this stale process must not mutate
# either artifact while handling its original failure.
FENCE_EXIT_ROOT="$TEST_ROOT/fence-loss-exit"
FENCE_EXIT_RELEASES="$FENCE_EXIT_ROOT/releases"
FENCE_EXIT_NEW="$FENCE_EXIT_RELEASES/v-partial"
FENCE_EXIT_POLL="$FENCE_EXIT_RELEASES/.mini-release-poll-control.json"
mkdir -p "$FENCE_EXIT_NEW"
printf 'keep partial release\n' > "$FENCE_EXIT_NEW/sentinel"
printf 'keep existing authorization\n' > "$FENCE_EXIT_POLL"
if (
  eval "$cleanup_body"
  DRY_RUN=0
  NEW_DIR="$FENCE_EXIT_NEW"
  LEASE_CUT_READY=1
  CURRENT_LINK="$FENCE_EXIT_ROOT/runtime-current"
  PRODUCTION_WRITE_LEASE_JSON='{"lease_id":"stale"}'
  production_write_mutation_allowed() { return 1; }
  freeze_managed_poll_after_failure() { : > "$FENCE_EXIT_ROOT/freeze-should-not-run"; return 0; }
  release_production_write_lease() { :; }
  cleanup_production_write_lease_bootstrap() { :; }
  release_cut_lock() { :; }
  warn() { :; }
  false
  cleanup_on_exit
); then
  fail "fence-loss cleanup unexpectedly returned success"
fi
[ -f "$FENCE_EXIT_NEW/sentinel" ] \
  || fail "fence-loss EXIT cleanup deleted NEW_DIR"
[ "$(cat "$FENCE_EXIT_POLL")" = 'keep existing authorization' ] \
  || fail "fence-loss EXIT cleanup mutated poll-control state"
[ ! -e "$FENCE_EXIT_ROOT/freeze-should-not-run" ] \
  || fail "fence-loss EXIT cleanup attempted poll freeze"

# ---------------------------------------------------------------------------
# runtime-current pointer health (ClickUp 86e2kt3yr).
#
# The 2026-08-02 incident left runtime-current as a BARE-NAME relative symlink
# (`v0.18.2-<sha>`), which resolves against $HERMES_HOME instead of releases/
# and therefore dangled. These checks pin every corruption shape the pointer
# contract must reject, and prove the healthy case still passes.
# ---------------------------------------------------------------------------
POINTER_ROOT="$TEST_ROOT/pointer"
POINTER_HOME="$POINTER_ROOT/.hermes"
POINTER_RELEASES="$POINTER_HOME/releases"
POINTER_ACTIVE="$POINTER_RELEASES/v9.9.9-abcdef123456"
mkdir -p "$POINTER_ACTIVE/venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$POINTER_ACTIVE/venv/bin/python"
chmod 0755 "$POINTER_ACTIVE/venv/bin/python"
POINTER_HOME="$(cd -P "$POINTER_HOME" && pwd -P)"
POINTER_RELEASES="$POINTER_HOME/releases"
POINTER_ACTIVE="$POINTER_RELEASES/v9.9.9-abcdef123456"
POINTER_LINK="$POINTER_HOME/runtime-current"

pointer_health() (
  HERMES_HOME="$POINTER_HOME"
  RELEASES_DIR="$POINTER_RELEASES"
  CURRENT_LINK="$POINTER_LINK"
  current_link_health
)
pointer_structure() (
  HERMES_HOME="$POINTER_HOME"
  RELEASES_DIR="$POINTER_RELEASES"
  CURRENT_LINK="$POINTER_LINK"
  current_link_structure_ok
)

# Missing pointer.
rm -f "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"runtime-current is missing"*) ;;
  *) fail "missing pointer was not reported as missing: $POINTER_OUT" ;;
esac
if pointer_health >/dev/null 2>&1; then fail "missing pointer reported healthy"; fi

# A regular directory in place of the pointer is not a symlink.
mkdir -p "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"runtime-current is not a symlink"*) ;;
  *) fail "non-symlink pointer was not detected: $POINTER_OUT" ;;
esac
rmdir "$POINTER_LINK"

# THE INCIDENT SHAPE: bare-name relative link. It must be rejected on the raw
# link text, before any resolution, and must never be reported healthy.
ln -sfn "v9.9.9-abcdef123456" "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"relative symlink"*) ;;
  *) fail "bare-name relative pointer was not rejected: $POINTER_OUT" ;;
esac
if pointer_health >/dev/null 2>&1; then fail "bare-name relative pointer reported healthy"; fi
if pointer_structure >/dev/null 2>&1; then fail "bare-name relative pointer passed the structural check"; fi

# A relative link that DOES resolve is still out of contract: the receipt
# records an absolute runtime_target and consumers string-join onto it.
ln -sfn "releases/v9.9.9-abcdef123456" "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"relative symlink"*) ;;
  *) fail "resolvable relative pointer was not rejected: $POINTER_OUT" ;;
esac

# Absolute but non-canonical (traversal through releases/..) is rejected.
ln -sfn "$POINTER_RELEASES/../releases/v9.9.9-abcdef123456" "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"not canonical"*) ;;
  *) fail "non-canonical absolute pointer was not rejected: $POINTER_OUT" ;;
esac

# Absolute link that escapes releases/ entirely.
mkdir -p "$POINTER_ROOT/outside"
ln -sfn "$POINTER_ROOT/outside" "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"escapes releases dir"*) ;;
  *) fail "escaping pointer was not rejected: $POINTER_OUT" ;;
esac

# Absolute, canonical, but dangling.
ln -sfn "$POINTER_RELEASES/v0.0.0-deadbeefcafe" "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"dangling"*) ;;
  *) fail "dangling pointer was not rejected: $POINTER_OUT" ;;
esac

# Structurally valid but the release has no usable runtime interpreter. The
# full health contract rejects it; the structural contract (used by the swap
# primitive itself) accepts it.
POINTER_NOVENV="$POINTER_RELEASES/v9.9.9-000000000000"
mkdir -p "$POINTER_NOVENV"
ln -sfn "$POINTER_NOVENV" "$POINTER_LINK"
POINTER_OUT="$(pointer_health || true)"
case "$POINTER_OUT" in
  *"no usable runtime Python"*) ;;
  *) fail "pointer with no runtime Python was not rejected: $POINTER_OUT" ;;
esac
pointer_structure >/dev/null 2>&1 \
  || fail "structural check rejected a release whose venv is absent"

# Healthy pointer: reports the resolved target and exits 0.
ln -sfn "$POINTER_ACTIVE" "$POINTER_LINK"
POINTER_OUT="$(pointer_health)" || fail "healthy pointer was reported corrupt: $POINTER_OUT"
[ "$POINTER_OUT" = "$POINTER_ACTIVE" ] \
  || fail "healthy pointer did not report its resolved target: $POINTER_OUT"

# assert_current_link_healthy must die (not warn) on a corrupt pointer, and
# must name the repair path so an operator is not left guessing.
ln -sfn "v9.9.9-abcdef123456" "$POINTER_LINK"
ASSERT_OUT="$(
  (
    HERMES_HOME="$POINTER_HOME"
    RELEASES_DIR="$POINTER_RELEASES"
    CURRENT_LINK="$POINTER_LINK"
    assert_current_link_healthy
  ) 2>&1 || true
)"
case "$ASSERT_OUT" in
  *"CORRUPT"*"--repair-pointer"*) ;;
  *) fail "assert_current_link_healthy did not report a corrupt pointer with a repair hint: $ASSERT_OUT" ;;
esac
if (
  HERMES_HOME="$POINTER_HOME"
  RELEASES_DIR="$POINTER_RELEASES"
  CURRENT_LINK="$POINTER_LINK"
  assert_current_link_healthy
) >/dev/null 2>&1; then
  fail "assert_current_link_healthy accepted a corrupt pointer"
fi

# repoint_symlink must fail closed when its own swap leaves a pointer that
# violates the contract. Simulate a faulty mv that writes a bare-name link
# with the right basename: target equality alone would have accepted it.
rm -f "$POINTER_LINK"
if (
  HERMES_HOME="$POINTER_HOME"
  RELEASES_DIR="$POINTER_RELEASES"
  CURRENT_LINK="$POINTER_LINK"
  DRY_RUN=0
  # shellcheck disable=SC2329 # invoked by repoint_symlink.
  guarded_or_direct() {
    if [ "${1:-}" = mv ]; then
      rm -f "${!#}" "${@: -2:1}"
      ln -sfn "v9.9.9-abcdef123456" "$CURRENT_LINK"
      return 0
    fi
    "$@"
  }
  repoint_symlink "$POINTER_ACTIVE"
) >/dev/null 2>&1; then
  fail "repoint_symlink accepted a bare-name pointer after the swap"
fi

# The happy path still succeeds and leaves an absolute canonical pointer.
rm -f "$POINTER_LINK"
(
  HERMES_HOME="$POINTER_HOME"
  RELEASES_DIR="$POINTER_RELEASES"
  CURRENT_LINK="$POINTER_LINK"
  DRY_RUN=0
  repoint_symlink "$POINTER_ACTIVE"
) >/dev/null || fail "repoint_symlink failed on a healthy target"
[ "$(readlink "$POINTER_LINK")" = "$POINTER_ACTIVE" ] \
  || fail "repoint_symlink did not leave an absolute canonical pointer"

# receipt_verified_runtime_target only trusts a receipt that byte-matches its
# content-addressed twin, so a hand-edited pointer cannot become a repair
# source.
RECEIPT_ROOT="$TEST_ROOT/receipt-repair"
RECEIPT_RELEASES="$RECEIPT_ROOT/releases"
mkdir -p "$RECEIPT_RELEASES/v9.9.9-abcdef123456"
RECEIPT_RELEASES="$(cd -P "$RECEIPT_RELEASES" && pwd -P)"
RECEIPT_ACTIVE="$RECEIPT_RELEASES/v9.9.9-abcdef123456"
RECEIPT_LAST="$RECEIPT_RELEASES/.mini-release-last-receipt.json"
printf '{"event":"cut","runtime_target":"%s","schema_version":2}\n' "$RECEIPT_ACTIVE" > "$RECEIPT_LAST"
RECEIPT_DIGEST="$(shasum -a 256 "$RECEIPT_LAST" | cut -d' ' -f1)"

# No content-addressed twin yet -> refuse.
if (
  RELEASES_DIR="$RECEIPT_RELEASES"
  LAST_RECEIPT_FILE="$RECEIPT_LAST"
  receipt_verified_runtime_target
) >/dev/null 2>&1; then
  fail "receipt repair source accepted a receipt with no content-addressed twin"
fi

cp "$RECEIPT_LAST" "$RECEIPT_RELEASES/.mini-release-receipt-$RECEIPT_DIGEST.json"
RECEIPT_OUT="$(
  RELEASES_DIR="$RECEIPT_RELEASES"
  LAST_RECEIPT_FILE="$RECEIPT_LAST"
  receipt_verified_runtime_target
)" || fail "receipt repair source rejected a valid content-addressed receipt"
[ "$RECEIPT_OUT" = "$RECEIPT_ACTIVE" ] \
  || fail "receipt repair source returned the wrong target: $RECEIPT_OUT"

# A receipt naming a target outside releases/ must never be a repair source.
printf '{"event":"cut","runtime_target":"%s","schema_version":2}\n' "$RECEIPT_ROOT" > "$RECEIPT_LAST"
RECEIPT_DIGEST="$(shasum -a 256 "$RECEIPT_LAST" | cut -d' ' -f1)"
cp "$RECEIPT_LAST" "$RECEIPT_RELEASES/.mini-release-receipt-$RECEIPT_DIGEST.json"
if (
  RELEASES_DIR="$RECEIPT_RELEASES"
  LAST_RECEIPT_FILE="$RECEIPT_LAST"
  receipt_verified_runtime_target
) >/dev/null 2>&1; then
  fail "receipt repair source accepted a target outside releases/"
fi

# Bundle control-plane files are governed too: changing a manifest or its
# installer must not deadlock the release gate that is responsible for
# deploying that bundle. An unrelated runtime source in the same target remains
# uncovered.
(
  ACTIVE_SHA=active
  SHA=target
  git_current() {
    case "${1:-}" in
      diff)
        printf '%s\n' \
          machine-setup/mini-scripts/spend_manifest.json \
          machine-setup/mini-scripts/install_spend.py \
          machine-setup/mini-scripts/forgotten_runtime.py
        ;;
      show) printf '%s\n' '{"files":[]}' ;;
      *) return 2 ;;
    esac
  }
  [ "$(find_uncovered_mini_scripts_changes)" = \
    'machine-setup/mini-scripts/forgotten_runtime.py' ]
) || fail "bundle manifest/installer controls were not admitted precisely"

# Release admission fails closed before build/switch when the target changes a
# Mini runtime file that is outside every governed manifest.  The same gate is
# a no-op for a fully covered target, so ordinary cuts remain admissible.
if (
  find_uncovered_mini_scripts_changes() {
    printf '%s\n' 'machine-setup/mini-scripts/forgotten_runtime.py'
  }
  require_governed_mini_scripts_changes
) >/dev/null 2>&1; then
  fail "release admission accepted an ungoverned Mini runtime change"
fi
(
  find_uncovered_mini_scripts_changes() { :; }
  require_governed_mini_scripts_changes
) || fail "release admission rejected a fully governed Mini runtime change set"

# Change discovery is an admission input, not optional evidence. A failed diff
# must reject the release instead of being converted into an empty change set.
if (
  ACTIVE_SHA=active
  SHA=target
  git_current() {
    [ "${1:-}" != diff ] || return 41
    return 2
  }
  require_governed_mini_scripts_changes
) >/dev/null 2>&1; then
  fail "release admission failed open when Mini change discovery failed"
fi

# Registry classifications are release-admission classifications too. Direct
# sources and explicit mini-local destinations are admitted; an unrelated
# changed runtime file remains uncovered.
(
  ACTIVE_SHA=active
  SHA=target
  git_current() {
    case "${1:-}" in
      diff)
        printf '%s\n' \
          machine-setup/mini-scripts/direct_tool.py \
          machine-setup/mini-scripts/local_tool.py \
          machine-setup/mini-scripts/launchd/com.example.local.plist \
          machine-setup/mini-scripts/forgotten_runtime.py
        ;;
      show)
        case "${2:-}" in
          *:machine-setup/mini-scripts/mini_local_registry.json)
            printf '%s\n' '{"direct_deploy":[{"src_rel":"direct_tool.py","dest":"scripts/direct_tool.py"}],"mini_local":[{"path":"scripts/local_tool.py"},{"path":"launch_agents/com.example.*","glob":true}]}'
            ;;
          *) printf '%s\n' '{"files":[]}' ;;
        esac
        ;;
      *) return 2 ;;
    esac
  }
  [ "$(find_uncovered_mini_scripts_changes)" = \
    'machine-setup/mini-scripts/forgotten_runtime.py' ]
) || fail "release admission ignored direct-deploy or mini-local registry classifications"

printf 'mini-release-cut safety checks passed\n'
