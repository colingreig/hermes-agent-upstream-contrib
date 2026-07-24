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
# shellcheck disable=SC1090 # SCRIPT is calculated from this test's location.
HERMES_HOME="$TEST_ROOT/home/.hermes" MINI_RELEASE_CUT_TEST_LIB=1 source "$SCRIPT"

RELEASES_DIR="$(canonical_existing_dir "$TEST_ROOT/home/.hermes/releases")"
PREV_FILE="$RELEASES_DIR/.previous"
CUT_LOCK_DIR="$RELEASES_DIR/.mini-release-cut.lock"
# shellcheck disable=SC2034 # referenced by helpers sourced from SCRIPT.
DRY_RUN=0

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

# A rollback whose gateway is healthy but dashboard remains unhealthy must
# terminate nonzero; a warning-only rollback would make this subshell succeed.
PREVIOUS_RELEASE="$RELEASES_DIR/v1.2.3-123456789abc"
mkdir "$PREVIOUS_RELEASE"
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

printf 'mini-release-cut safety checks passed\n'
