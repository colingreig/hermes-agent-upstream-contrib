#!/usr/bin/env bash
# Focused, dependency-free safety checks for scripts/mini-release-cut.sh.
set -euo pipefail

SCRIPT_DIR="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT="$SCRIPT_DIR/../../scripts/mini-release-cut.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/mini-release-cut-test.XXXXXX")"

cleanup() {
  rm -rf "$TEST_ROOT"
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

# mkdir-based locking rejects a second release-cut owner until the first one
# releases it. (The second call runs in a subprocess so its deliberate die()
# does not terminate this harness.)
acquire_cut_lock
expect_failure acquire_cut_lock
release_cut_lock
[ ! -e "$CUT_LOCK_DIR" ] || fail "release-cut lock was not removed"

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

# A rollback whose gateway is healthy but dashboard remains unhealthy must
# terminate nonzero; a warning-only rollback would make this subshell succeed.
PREVIOUS_RELEASE="$RELEASES_DIR/v1.2.3-123456789abc"
mkdir -p "$PREVIOUS_RELEASE/scripts" "$PREVIOUS_RELEASE/$(dirname "$VENDORED_REFRESH_REL")"
printf '#!/usr/bin/env bash\nprintf previous\n' > "$PREVIOUS_RELEASE/scripts/cu-clickup"
chmod 0755 "$PREVIOUS_RELEASE/scripts/cu-clickup"
printf '#!/usr/bin/env python3\nprint("previous")\n' > "$PREVIOUS_RELEASE/$VENDORED_REFRESH_REL"
printf '%s\n' "$PREVIOUS_RELEASE" > "$PREV_FILE"
if (
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  repoint_symlink() { :; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  kickstart() { :; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_gateway() { return 0; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_dashboard() { return 1; }
  rollback_to_previous 'safety harness'
) >/dev/null 2>&1; then
  fail 'rollback returned success despite dashboard health failure'
fi

# Either service can fail to kickstart immediately after runtime-current is
# switched. Both failures must restore the recorded previous target and still
# report a failed cut after rollback succeeds.
NEW_RELEASE="$RELEASES_DIR/v1.2.4-abcdef123456"
mkdir "$NEW_RELEASE"
for failed_service in gateway dashboard; do
  repoint_symlink "$NEW_RELEASE" >/dev/null
  failed_target="$GATEWAY_TARGET"
  [ "$failed_service" = dashboard ] && failed_target="$DASHBOARD_TARGET"
  if (
    kickstart_calls=0
    # Fail only the post-switch restart; rollback restarts then succeed.
    # shellcheck disable=SC2329 # invoked by kickstart_after_switch/rollback_to_previous.
    kickstart() {
      kickstart_calls=$((kickstart_calls + 1))
      [ "$kickstart_calls" -ne 1 ]
    }
    # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
    verify_gateway() { return 0; }
    # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
    verify_dashboard() { return 0; }
    kickstart_after_switch "$failed_target" "$failed_service"
  ) >/dev/null 2>&1; then
    fail "$failed_service kickstart failure returned success after rollback"
  fi
  [ "$(readlink "$CURRENT_LINK")" = "$PREVIOUS_RELEASE" ] \
    || fail "$failed_service kickstart failure left runtime-current on the new release"
done

# Receipt path validation failures occur after the runtime and governed script
# have switched. They must return through the caller's rollback branch rather
# than exiting from a nested path assertion and leaving the new release live.
repoint_symlink "$NEW_RELEASE" >/dev/null
printf '#!/usr/bin/env python3\nprint("new live")\n' > "$DEPLOYED_REFRESH"
RECEIPT_OUTSIDE="$TEST_ROOT/receipt-outside"
printf 'must remain untouched\n' > "$RECEIPT_OUTSIDE"
rm -f "$LAST_RECEIPT_FILE"
ln -s "$RECEIPT_OUTSIDE" "$LAST_RECEIPT_FILE"
if (
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  kickstart() { :; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_gateway() { return 0; }
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  verify_dashboard() { return 0; }
  # Keep this regression focused on runtime/refresh restoration.
  # shellcheck disable=SC2329 # invoked indirectly by rollback_to_previous.
  install_clickup_cli() { :; }
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
      machine-setup/mini-scripts/reconcile_fleet_outcomes.py)
        printf '100755 blob %s\t%s\n' "$DRY_TARGET_BLOB" "$target_path"
        ;;
      machine-setup/mini-scripts/fleet_outcome_manifest.json)
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

# Installer preflight must fetch while holding the real release lock. Begin
# with a stale origin ref equal to HEAD; the fake fetch advances it to a
# divergent commit. A dry-run-style preflight would incorrectly install,
# while the real preflight must reject and leave LaunchAgents untouched.
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
    [ -d "$FAKE_RELEASES_DIR/.mini-release-cut.lock" ] || exit 90
    printf 'locked\n' > "$FAKE_FETCH_MARKER"
    printf '%s\n' "$FAKE_REMOTE_SHA" > "$FAKE_GIT_STATE"
    exit 0
    ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    -C|-c) shift 2 ;;
    *) break ;;
  esac
done
case "${1:-}" in
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
  FAKE_ACTIVE_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  FAKE_REMOTE_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  "$POLL_INSTALLER" --install \
    --certified-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    --promotion-receipt-id cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc \
    >/dev/null 2>&1; then
  fail "installer accepted stale local equality after current remote diverged"
fi
[ -f "$PREFLIGHT_MARKER" ] \
  || fail "installer preflight did not fetch while holding the release lock"
[ ! -e "$PREFLIGHT_HERMES/releases/.mini-release-cut.lock" ] \
  || fail "installer preflight left the release lock behind"
[ ! -e "$PREFLIGHT_HOME/Library/LaunchAgents/com.colingreig.hermes.release-poll.plist" ] \
  || fail "installer wrote the LaunchAgent after divergent preflight"
[ ! -e "$PREFLIGHT_HERMES/releases/.mini-release-last-receipt.json" ] \
  || fail "installer preflight wrote a release receipt"

printf 'mini-release-cut safety checks passed\n'
