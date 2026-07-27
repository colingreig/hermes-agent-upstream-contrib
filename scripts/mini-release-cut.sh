#!/usr/bin/env bash
#
# mini-release-cut.sh — safe, repeatable release cut for the Hermes Mac mini.
#
# WHY THIS EXISTS
# ---------------
# On 2026-07-19 an *improvised* cutover to a
#   ~/.hermes/releases/<ver>-<sha>/  +  ~/.hermes/runtime-current symlink
# layout destroyed runtime state: SQLite DBs were truncated under live WAL
# connections, and config.yaml / the auth token / LaunchAgents were deleted.
# No committed automation produced that layout, so it could not be reviewed
# or reproduced. This script IS that automation. It builds a brand-new
# release directory in full, verifies it, and only then atomically repoints
# the `runtime-current` symlink and restarts the services. It NEVER mutates
# persistent runtime state (DBs, config, cron, logs, LaunchAgents). The sole
# operational-file exception is an exact-path, hash-verified, rollback-safe
# deployment of clickup_workspace_refresh.py.
#
# Tracked in ClickUp 86e2ddah5; conditional polling/governed script deployment
# added under 86e2gdfwc.
#
# TARGET: the Hermes Mac mini (macOS, uv-managed venv, node at /opt/homebrew).
# Do NOT run this anywhere else.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
RELEASES_DIR="$HERMES_HOME/releases"
CURRENT_LINK="$HERMES_HOME/runtime-current"
PREV_FILE="$RELEASES_DIR/.previous"
CUT_LOCK_DIR="$RELEASES_DIR/.mini-release-cut.lock"
LAST_RECEIPT_FILE="$RELEASES_DIR/.mini-release-last-receipt.json"
REFRESH_BACKUP_FILE="$RELEASES_DIR/.clickup_workspace_refresh.previous"
GATEWAY_LOG="$HERMES_HOME/logs/gateway.log"
LOCAL_BIN_DIR="$HOME/.local/bin"
CLICKUP_CLI_PATH_DIR="/opt/homebrew/bin"
CLICKUP_CLI_NAME="cu-clickup"
VENDORED_REFRESH_REL="machine-setup/mini-scripts/clickup_workspace_refresh.py"
DEPLOYED_REFRESH="$HERMES_HOME/scripts/clickup_workspace_refresh.py"

UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"
GATEWAY_LABEL="ai.hermes.gateway"
DASHBOARD_LABEL="com.colingreig.hermes-dashboard"
GATEWAY_TARGET="${GUI_DOMAIN}/${GATEWAY_LABEL}"
DASHBOARD_TARGET="${GUI_DOMAIN}/${DASHBOARD_LABEL}"

GATEWAY_PORT=8642
DASHBOARD_PORT=9119
MIN_PLATFORMS=2
VERIFY_TIMEOUT=60          # seconds
KEEP_RELEASES=3

# node/npm live in Homebrew, and uv lives at ~/.local/bin — neither is on a
# non-interactive ssh PATH.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:${PATH:-}"

DEFAULT_REF="prod-live-patches"

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
REF="$DEFAULT_REF"
DO_ROLLBACK=0
DRY_RUN=0
DO_PRUNE=0
OFFLINE=0
IF_ADVANCED=0
PREFLIGHT=0

usage() {
  cat <<'EOF'
Usage: mini-release-cut.sh [--ref <branch-or-sha>] [--if-advanced] [--preflight] [--rollback] [--prune] [--dry-run] [--offline]

  --ref <ref>   Branch or sha to cut (default: prod-live-patches).
  --if-advanced Cut only when the resolved ref is a strict descendant of the
                active runtime commit. Equal is a successful structured no-op;
                behind or diverged refs fail closed.
  --preflight   Acquire the real cut lock, fetch origin, and validate the
                requested ref as equal or a strict descendant, then exit
                without building, switching, or writing a receipt.
  --rollback    Repoint runtime-current to the previous release and restart.
                No build. Uses ~/.hermes/releases/.previous.
  --prune       After a successful cut, delete releases older than the newest
                3 (never the active or previous release). Off by default.
  --dry-run     Print every mutating action without performing it.
  --offline     Clone the new release from the local runtime-current clone
                instead of the network origin. runtime-current is normally a
                blobless partial clone, so this mode can only produce a
                complete tree for blobs it has already fetched on demand —
                the post-checkout integrity check will catch and fail on any
                gap rather than silently shipping a corrupt release. Prefer
                the default network clone; use this only when origin is
                genuinely unreachable.
EOF
}

while [ $# -gt 0 ]; do
  case "${1:-}" in
    --ref)      REF="${2:-}"; shift 2 ;;
    --ref=*)    REF="${1#*=}"; shift ;;
    --rollback) DO_ROLLBACK=1; shift ;;
    --prune)    DO_PRUNE=1; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --offline)  OFFLINE=1; shift ;;
    --if-advanced) IF_ADVANCED=1; shift ;;
    --preflight) PREFLIGHT=1; IF_ADVANCED=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "ERROR: unknown argument: ${1:-}" >&2; usage >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { printf '\033[36m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# run CMD... — echo it; execute unless dry-run.
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m %s\n' "$*"
    return 0
  fi
  "$@"
}

# Resolve an existing directory without relying on GNU-only realpath flags.
# RELEASES_DIR itself must be a real directory before the script is allowed to
# create a release beneath it, so resolving its parent is sufficient to make
# targets that do not exist yet safe as well.
canonical_existing_dir() {
  local dir="${1:-}"
  [ -n "$dir" ] && [ -d "$dir" ] || return 1
  (cd -P -- "$dir" && pwd -P)
}

# A release target is one direct child of the canonical releases directory.
# This rejects traversal (including a deceptively harmless-looking `foo/..`)
# before any create/remove, then reconstructs the target from the canonical
# parent and basename. Call this immediately before every such operation.
assert_release_target() {
  local target="${1:-}" parent base resolved_parent
  [ -n "$target" ] || die "SAFETY: empty release target"
  parent="$(dirname -- "$target")"
  base="$(basename -- "$target")"
  case "$base" in
    ''|.|..) die "SAFETY: invalid release target component: $target" ;;
  esac
  resolved_parent="$(canonical_existing_dir "$parent")" \
    || die "SAFETY: release target parent does not exist: $target"
  [ "$resolved_parent" = "$RELEASES_DIR" ] \
    || die "SAFETY: release target parent is not releases dir: $target (resolved: $resolved_parent)"
  [ "$target" = "$RELEASES_DIR/$base" ] \
    || die "SAFETY: release target is not canonical: $target"
}

release_target() {
  local component="${1:-}"
  case "$component" in
    ''|.|..|*/*) die "SAFETY: release name must be exactly one path component: $component" ;;
  esac
  printf '%s/%s\n' "$RELEASES_DIR" "$component"
}

# Versions are consumed as a filesystem component, so accept only the
# ASCII subset used by PEP 440: it must begin with a decimal release digit and
# may then contain letters, digits, dot, plus, underscore, hyphen, or epoch
# bang. This excludes whitespace/control bytes, shell punctuation, slashes,
# and option-looking values before they ever reach a path or command.
valid_release_version() {
  local version="${1:-}"
  [[ "$version" =~ ^[0-9][0-9A-Za-z.!+_-]*$ ]]
}

# A release-owned file may be replaced, but never through a symlink. This
# prevents a stale or malicious .previous link from redirecting a write out of
# releases/ after its parent was checked.
assert_regular_release_file() {
  local target="${1:-}"
  assert_release_target "$target"
  [ ! -L "$target" ] \
    || die "SAFETY: refusing to overwrite symlinked release file: $target"
}

# HARD SAFETY INVARIANT: forbidden live-state paths must never be written by
# this script. This is a belt-and-suspenders guard used by assertions.
FORBIDDEN=(
  "$HERMES_HOME/config.yaml"
  "$HERMES_HOME/cron"
  "$HERMES_HOME/scripts"
  "$HERMES_HOME/logs"
  "$HERMES_HOME/recovery"
  "$HOME/.config"
  "$HOME/Library/LaunchAgents"
)
assert_not_forbidden() {
  local p="${1:-}" f
  for f in "${FORBIDDEN[@]}"; do
    case "$p" in
      "$f"|"$f"/*) die "SAFETY: refusing to write forbidden live-state path: $p" ;;
    esac
  done
  case "$p" in
    "$HERMES_HOME"/*.db|"$HERMES_HOME"/*.db-*) die "SAFETY: refusing to touch a database: $p" ;;
  esac
}

git_current() { git -C "$CURRENT_LINK" "$@"; }

sha256_file() {
  local path="${1:-}"
  [ -f "$path" ] || return 1
  shasum -a 256 "$path" | awk '{print $1}'
}

# Classify a candidate relative to the active runtime commit. The caller has
# already fetched and resolved both values while holding CUT_LOCK_DIR.
classify_ref_advancement() {
  local active_sha="${1:-}" target_sha="${2:-}"
  [ -n "$active_sha" ] && [ -n "$target_sha" ] \
    || die "cannot classify advancement with an empty commit"
  if [ "$active_sha" = "$target_sha" ]; then
    printf 'equal\n'
  elif git_current merge-base --is-ancestor "$active_sha" "$target_sha"; then
    printf 'advance\n'
  elif git_current merge-base --is-ancestor "$target_sha" "$active_sha"; then
    printf 'behind\n'
  else
    printf 'diverged\n'
  fi
}

# Persist a deterministic, content-addressed receipt. The payload deliberately
# contains state, source identities, and hashes but no wall-clock timestamp:
# repeated no-op polls for the same state reuse the same immutable receipt.
# launchd's append-only stdout supplies observation timestamps.
write_release_receipt() {
  local event="${1:-}" from_sha="${2:-}" to_sha="${3:-}"
  local runtime_dir="${4:-}" source_hash="${5:-}" deployed_hash="${6:-}"
  local detail="${7:-}"
  local payload receipt_hash receipt_file tmp last_tmp

  case "$event" in
    noop|rejected|advanced|cut|rollback) ;;
    *) warn "invalid release receipt event: $event"; return 1 ;;
  esac
  [[ "$from_sha" =~ ^[0-9a-f]{40,64}$ ]] \
    && [[ "$to_sha" =~ ^[0-9a-f]{40,64}$ ]] || {
      warn "release receipt commit identity is not a full lowercase object id"
      return 1
    }
  [ -z "$source_hash" ] || [[ "$source_hash" =~ ^[0-9a-f]{64}$ ]] \
    || { warn "invalid governed refresh source SHA-256"; return 1; }
  [ -z "$deployed_hash" ] || [[ "$deployed_hash" =~ ^[0-9a-f]{64}$ ]] \
    || { warn "invalid governed refresh deployed SHA-256"; return 1; }
  # Receipt validation must be catchable by post-switch callers. The generic
  # path assertions deliberately call die(), which is appropriate before a
  # switch but would exit the whole process here before the caller can roll
  # back. Isolate them in a subshell so an explicit die becomes a normal
  # nonzero result.
  if ! (
    assert_release_target "$runtime_dir"
    assert_regular_release_file "$LAST_RECEIPT_FILE"
  ); then
    warn "release receipt target validation failed"
    return 1
  fi

  payload="$(python3 - "$event" "$REF" "$from_sha" "$to_sha" "$runtime_dir" \
    "$source_hash" "$deployed_hash" "$detail" <<'PY'
import json
import sys

event, ref, from_sha, to_sha, runtime_dir, source_hash, deployed_hash, detail = sys.argv[1:]
print(json.dumps({
    "schema_version": 1,
    "event": event,
    "ref": ref,
    "from_commit": from_sha,
    "to_commit": to_sha,
    "runtime_target": runtime_dir,
    "refresh_source_sha256": source_hash,
    "refresh_deployed_sha256": deployed_hash,
    "detail": detail,
}, sort_keys=True, separators=(",", ":")))
PY
  )" || return 1
  receipt_hash="$(printf '%s\n' "$payload" | shasum -a 256 | awk '{print $1}')"
  [ "${#receipt_hash}" -eq 64 ] || return 1
  receipt_file="$RELEASES_DIR/.mini-release-receipt-${receipt_hash}.json"
  if ! ( assert_regular_release_file "$receipt_file" ); then
    warn "release receipt addressed target validation failed: $receipt_file"
    return 1
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m receipt sha256=%s %s\n' "$receipt_hash" "$payload"
    return 0
  fi

  if [ ! -f "$receipt_file" ]; then
    tmp="$(mktemp "$RELEASES_DIR/.mini-release-receipt.swap.XXXXXX")" || return 1
    printf '%s\n' "$payload" > "$tmp" || { rm -f "$tmp"; return 1; }
    chmod 0644 "$tmp" || { rm -f "$tmp"; return 1; }
    mv -fh "$tmp" "$receipt_file" || { rm -f "$tmp"; return 1; }
  fi
  [ "$(sha256_file "$receipt_file")" = "$receipt_hash" ] || {
    warn "content-addressed receipt hash mismatch: $receipt_file"
    return 1
  }
  last_tmp="$(mktemp "$RELEASES_DIR/.mini-release-last.swap.XXXXXX")" || return 1
  cp "$receipt_file" "$last_tmp" || { rm -f "$last_tmp"; return 1; }
  chmod 0644 "$last_tmp" || { rm -f "$last_tmp"; return 1; }
  mv -fh "$last_tmp" "$LAST_RECEIPT_FILE" || { rm -f "$last_tmp"; return 1; }
  ok "release receipt sha256=$receipt_hash event=$event"
}

# The only governed ~/.hermes/scripts write. Source and destination must both
# be regular non-symlink files, the destination parent must resolve to the
# exact protected scripts directory, and replacement is a same-directory
# atomic rename. No other caller is allowed to bypass assert_not_forbidden.
install_governed_refresh_from_source() {
  local source="${1:-}" scripts_dir target tmp expected actual
  [ -f "$source" ] && [ ! -L "$source" ] || {
    warn "governed refresh source missing or symlinked: $source"
    return 1
  }
  scripts_dir="$(canonical_existing_dir "$HERMES_HOME/scripts")" || {
    warn "protected scripts directory missing: $HERMES_HOME/scripts"
    return 1
  }
  [ "$scripts_dir" = "$HERMES_HOME/scripts" ] || {
    warn "protected scripts directory is not canonical: $HERMES_HOME/scripts -> $scripts_dir"
    return 1
  }
  target="$scripts_dir/clickup_workspace_refresh.py"
  [ ! -L "$target" ] || {
    warn "refusing to replace symlinked governed refresh script: $target"
    return 1
  }
  expected="$(sha256_file "$source")" || return 1

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m atomic install %s -> %s (sha256=%s)\n' \
      "$source" "$target" "$expected"
    return 0
  fi

  tmp="$(mktemp "$scripts_dir/.clickup_workspace_refresh.swap.XXXXXX")" || return 1
  cp "$source" "$tmp" || { rm -f "$tmp"; return 1; }
  chmod 0755 "$tmp" || { rm -f "$tmp"; return 1; }
  actual="$(sha256_file "$tmp")" || { rm -f "$tmp"; return 1; }
  [ "$actual" = "$expected" ] || {
    rm -f "$tmp"
    warn "governed refresh staging hash mismatch"
    return 1
  }
  mv -fh "$tmp" "$target" || { rm -f "$tmp"; return 1; }
  [ ! -L "$target" ] && [ "$(sha256_file "$target")" = "$expected" ] || {
    warn "governed refresh post-install verification failed: $target"
    return 1
  }
  ok "governed refresh installed: $target (sha256=$expected)"
}

install_governed_refresh() {
  local release_dir="${1:-}"
  install_governed_refresh_from_source "$release_dir/$VENDORED_REFRESH_REL"
}

# Save the exact pre-cut deployed bytes under releases/ before any switch. A
# previous release created before vendoring may not contain the governed source,
# so this backup is the rollback authority for the bootstrap cut.
stage_refresh_backup() {
  local source="$DEPLOYED_REFRESH" tmp expected
  [ -f "$source" ] && [ ! -L "$source" ] \
    || die "governed deployed refresh missing or symlinked: $source"
  assert_regular_release_file "$REFRESH_BACKUP_FILE"
  expected="$(sha256_file "$source")" || die "could not hash deployed refresh: $source"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m backup %s -> %s (sha256=%s)\n' \
      "$source" "$REFRESH_BACKUP_FILE" "$expected"
    return 0
  fi
  tmp="$(mktemp "$RELEASES_DIR/.clickup-refresh-backup.swap.XXXXXX")" \
    || die "could not create governed refresh backup"
  cp "$source" "$tmp" || { rm -f "$tmp"; die "could not stage governed refresh backup"; }
  chmod 0644 "$tmp" || { rm -f "$tmp"; die "could not secure governed refresh backup"; }
  [ "$(sha256_file "$tmp")" = "$expected" ] \
    || { rm -f "$tmp"; die "governed refresh backup hash mismatch"; }
  mv -fh "$tmp" "$REFRESH_BACKUP_FILE" \
    || { rm -f "$tmp"; die "could not atomically record governed refresh backup"; }
}

restore_governed_refresh_for_release() {
  local release_dir="${1:-}"
  local source="$release_dir/$VENDORED_REFRESH_REL"
  if [ ! -f "$source" ]; then
    source="$REFRESH_BACKUP_FILE"
  fi
  install_governed_refresh_from_source "$source"
}

# Install the release-owned ClickUp wrapper as a stable user command.  The
# wrapper itself calls the protected live refresh script, so it remains valid
# across runtime-current switches and rollbacks.  Reinstalling it after every
# successful cut repairs accidental deletion without mutating cron, secrets,
# or any other protected live state.
install_clickup_cli() {
  local release_dir="${1:-}"
  local source="$release_dir/scripts/$CLICKUP_CLI_NAME"
  local bin_dir path_dir target path_target tmp path_swap_dir path_tmp

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m install -m 0755 %s %s/%s; link %s/%s (atomic replace)\n' \
      "$source" "$LOCAL_BIN_DIR" "$CLICKUP_CLI_NAME" \
      "$CLICKUP_CLI_PATH_DIR" "$CLICKUP_CLI_NAME"
    return 0
  fi

  [ -f "$source" ] || {
    warn "managed ClickUp CLI source missing: $source"
    return 1
  }
  bin_dir="$(canonical_existing_dir "$LOCAL_BIN_DIR")" || {
    warn "managed command directory missing: $LOCAL_BIN_DIR"
    return 1
  }
  path_dir="$(canonical_existing_dir "$CLICKUP_CLI_PATH_DIR")" || {
    warn "managed PATH directory missing: $CLICKUP_CLI_PATH_DIR"
    return 1
  }
  target="$bin_dir/$CLICKUP_CLI_NAME"
  path_target="$path_dir/$CLICKUP_CLI_NAME"
  tmp="$(mktemp "$bin_dir/.${CLICKUP_CLI_NAME}.swap.XXXXXX")" || return 1

  if ! cp "$source" "$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  chmod 0755 "$tmp" || {
    rm -f "$tmp"
    return 1
  }
  if ! mv -fh "$tmp" "$target"; then
    rm -f "$tmp"
    return 1
  fi
  [ -x "$target" ] || {
    warn "managed ClickUp CLI is not executable after install: $target"
    return 1
  }
  cmp -s "$source" "$target" || {
    warn "managed ClickUp CLI verification failed: $target differs from release source"
    return 1
  }
  path_swap_dir="$(mktemp -d "$path_dir/.${CLICKUP_CLI_NAME}.swap.XXXXXX")" || return 1
  path_tmp="$path_swap_dir/$CLICKUP_CLI_NAME"
  ln -s "$target" "$path_tmp" || {
    rmdir "$path_swap_dir"
    return 1
  }
  if ! mv -fh "$path_tmp" "$path_target"; then
    rm -f "$path_tmp"
    rmdir "$path_swap_dir"
    return 1
  fi
  rmdir "$path_swap_dir" || warn "could not remove managed CLI swap dir: $path_swap_dir"
  [ -L "$path_target" ] && [ "$(readlink "$path_target")" = "$target" ] || {
    warn "managed ClickUp CLI PATH link verification failed: $path_target"
    return 1
  }
  ok "managed ClickUp CLI installed: $target (PATH link: $path_target)"
}

port_listening() {
  local port="${1:-}"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

http_ok() {
  local url="${1:-}"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || echo 000)"
  [ "$code" = "200" ]
}

# ---------------------------------------------------------------------------
# Symlink repoint (atomic) — one of the two permitted out-of-releases writes.
# ---------------------------------------------------------------------------
repoint_symlink() {
  local target="${1:-}"
  [ -n "$target" ] || die "repoint_symlink: empty target"
  assert_release_target "$target"
  local tmp="${CURRENT_LINK}.swap.$$"
  if [ "$DRY_RUN" -eq 1 ]; then
    # The build was dry-run-skipped, so $target won't exist yet — don't assert.
    printf '\033[35m[DRY-RUN]\033[0m ln -sfn %s %s && mv -fh %s %s\n' "$target" "$tmp" "$tmp" "$CURRENT_LINK"
    return 0
  fi
  [ -d "$target" ] || die "repoint_symlink: target is not a directory: $target"
  ln -sfn "$target" "$tmp"
  # -h: do NOT follow CURRENT_LINK even though it is a symlink to a
  # directory. Without -h, macOS/BSD mv(1) treats an existing
  # symlink-that-resolves-to-a-directory destination as its "second form"
  # (move source INTO that directory) rather than replacing the symlink —
  # so $tmp would silently land inside the *current* release dir instead of
  # ever repointing CURRENT_LINK, while this function still reported
  # success. -h forces "rename source to target" instead; same filesystem
  # means this is still a plain rename(2) under the hood, i.e. still atomic.
  mv -fh "$tmp" "$CURRENT_LINK"
  # Belt-and-suspenders: don't just trust the exit code — confirm the swap
  # actually took effect before declaring success (this is exactly the
  # invariant that was silently violated before the -h fix above).
  [ "$(readlink "$CURRENT_LINK")" = "$target" ] \
    || die "repoint_symlink: swap did not take effect (runtime-current still -> $(readlink "$CURRENT_LINK" 2>/dev/null))"
  ok "runtime-current → $target"
}

# ---------------------------------------------------------------------------
# Service restart — the other permitted out-of-releases action.
# ---------------------------------------------------------------------------
kickstart() {
  local target="${1:-}"
  log "restart: launchctl kickstart -k $target"
  run launchctl kickstart -k "$target"
}

# mkdir is atomic, unlike checking then creating a pid file. The lock covers
# cuts, explicit rollbacks, and pruning so two operators cannot race the
# runtime-current switch or delete one another's release.
LOCK_HELD=0
acquire_cut_lock() {
  assert_release_target "$CUT_LOCK_DIR"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m mkdir %s (single-instance cut lock)\n' "$CUT_LOCK_DIR"
    return 0
  fi
  if ! mkdir "$CUT_LOCK_DIR"; then
    die "another mini-release-cut is already running (lock: $CUT_LOCK_DIR)"
  fi
  LOCK_HELD=1
  ok "acquired single-instance release-cut lock"
}

# shellcheck disable=SC2329 # called by the EXIT trap installed below
release_cut_lock() {
  [ "$LOCK_HELD" -eq 1 ] || return 0
  # Prove the resolved parent again immediately before removing the lock.
  assert_release_target "$CUT_LOCK_DIR"
  rmdir "$CUT_LOCK_DIR" || warn "could not remove release-cut lock: $CUT_LOCK_DIR"
  LOCK_HELD=0
}

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
# Return the byte offset of the gateway log so we only scan lines emitted
# AFTER a restart (the log accumulates across restarts).
log_offset() {
  if [ -f "$GATEWAY_LOG" ]; then
    wc -c < "$GATEWAY_LOG" | tr -d ' '
  else
    echo 0
  fi
}

# Scan new gateway.log content (from $1 bytes onward) for
#   "Gateway running with N platform(s)"  with N >= MIN_PLATFORMS.
gateway_platforms_ready() {
  local offset="${1:-0}" line count
  [ -f "$GATEWAY_LOG" ] || return 1
  line="$(tail -c "+$((offset + 1))" "$GATEWAY_LOG" 2>/dev/null \
            | grep -Eo 'Gateway running with [0-9]+ platform\(s\)' | tail -n1 || true)"
  [ -n "$line" ] || return 1
  count="$(printf '%s' "$line" | grep -Eo '[0-9]+' | head -n1)"
  [ -n "$count" ] && [ "$count" -ge "$MIN_PLATFORMS" ]
}

# Verify the gateway came up on $1 (release dir) after a restart begun at
# byte offset $2 in the gateway log. Polls up to VERIFY_TIMEOUT.
verify_gateway() {
  local release_dir="${1:-}" offset="${2:-0}"
  local deadline=$((SECONDS + VERIFY_TIMEOUT))
  local proc_ok=0 plat_ok=0 port_ok=0 link_ok=0
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m verify gateway: runtime-current -> %s, proc via runtime-current, >=%s platform(s), :%s listening\n' \
      "$release_dir" "$MIN_PLATFORMS" "$GATEWAY_PORT"
    return 0
  fi
  log "verifying gateway (up to ${VERIFY_TIMEOUT}s)…"
  while [ "$SECONDS" -lt "$deadline" ]; do
    proc_ok=0; plat_ok=0; port_ok=0; link_ok=0
    # The LaunchAgent's ProgramArguments are generated against the
    # `runtime-current` symlink path (see hermes_cli/gateway.py's plist
    # generator), not the literal per-release directory — so pgrep/ps only
    # ever observe "runtime-current" in argv, never $release_dir itself.
    # Confirm the symlink currently resolves to the expected release AND
    # match the process via the stable symlink-relative command line.
    [ -L "$CURRENT_LINK" ] && [ "$(readlink "$CURRENT_LINK")" = "$release_dir" ] && link_ok=1
    pgrep -f "${CURRENT_LINK}/venv/bin/python.*gateway run" >/dev/null 2>&1 && proc_ok=1
    gateway_platforms_ready "$offset" && plat_ok=1
    port_listening "$GATEWAY_PORT" && port_ok=1
    if [ "$link_ok" = 1 ] && [ "$proc_ok" = 1 ] && [ "$plat_ok" = 1 ] && [ "$port_ok" = 1 ]; then
      ok "gateway healthy (runtime-current → $release_dir, proc matches, >=${MIN_PLATFORMS} platforms, :${GATEWAY_PORT} listening)"
      return 0
    fi
    sleep 2
  done
  warn "gateway verify failed: link=$link_ok proc=$proc_ok platforms=$plat_ok port=$port_ok"
  return 1
}

verify_dashboard() {
  local deadline=$((SECONDS + VERIFY_TIMEOUT))
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m verify dashboard: HTTP 200 on http://127.0.0.1:%s\n' "$DASHBOARD_PORT"
    return 0
  fi
  log "verifying dashboard (up to ${VERIFY_TIMEOUT}s)…"
  while [ "$SECONDS" -lt "$deadline" ]; do
    if http_ok "http://127.0.0.1:${DASHBOARD_PORT}"; then
      ok "dashboard healthy (HTTP 200 on :${DASHBOARD_PORT})"
      return 0
    fi
    sleep 2
  done
  warn "dashboard verify failed (no HTTP 200 on :${DASHBOARD_PORT})"
  return 1
}

# ---------------------------------------------------------------------------
# Rollback: repoint to the recorded previous release and restart+verify.
# ---------------------------------------------------------------------------
rollback_to_previous() {
  local reason="${1:-manual}"
  [ -f "$PREV_FILE" ] || die "cannot rollback: $PREV_FILE not found"
  local prev
  prev="$(cat "$PREV_FILE")"
  [ -n "$prev" ] || die "cannot rollback: $PREV_FILE is empty"
  assert_release_target "$prev"
  [ -d "$prev" ] || die "cannot rollback: previous release missing: $prev"
  warn "ROLLBACK ($reason) → $prev"
  local offset
  offset="$(log_offset)"
  repoint_symlink "$prev"
  restore_governed_refresh_for_release "$prev" \
    || die "rollback could not restore governed ClickUp refresh — MANUAL INTERVENTION REQUIRED"
  install_clickup_cli "$prev" \
    || die "rollback could not restore managed ClickUp CLI — MANUAL INTERVENTION REQUIRED"
  kickstart "$GATEWAY_TARGET"
  if verify_gateway "$prev" "$offset"; then
    kickstart "$DASHBOARD_TARGET"
    verify_dashboard || die "rollback dashboard did NOT verify healthy — MANUAL INTERVENTION REQUIRED (release: $prev)"
    ok "rollback complete → $prev"
    return 0
  fi
  die "rollback restart did NOT verify healthy — MANUAL INTERVENTION REQUIRED (release: $prev)"
}

record_cut_receipt_or_rollback() {
  local event="${1:-}" from_sha="${2:-}" to_sha="${3:-}"
  local runtime_dir="${4:-}" source_hash="${5:-}" deployed_hash="${6:-}"
  local detail="${7:-}"
  if ! write_release_receipt "$event" "$from_sha" "$to_sha" "$runtime_dir" \
    "$source_hash" "$deployed_hash" "$detail"; then
    warn "release receipt recording failed — rolling back"
    rollback_to_previous "release receipt recording failed"
    die "cut aborted and rolled back to previous release"
  fi
}

# A post-switch restart failure must be handled explicitly: under `set -e`, a
# bare kickstart would otherwise exit before the new runtime target can be
# rolled back. This helper always leaves a failed cut on the previous release
# and still terminates nonzero after a successful rollback.
kickstart_after_switch() {
  local target="${1:-}" service="${2:-service}"
  if ! kickstart "$target"; then
    warn "$service did not restart on new release — rolling back"
    rollback_to_previous "$service kickstart failed"
    die "cut aborted and rolled back to previous release ($service kickstart failed)"
  fi
}

# ---------------------------------------------------------------------------
# Prune: keep the newest KEEP_RELEASES; never remove active or previous.
# ---------------------------------------------------------------------------
prune_releases() {
  log "prune: keeping newest $KEEP_RELEASES release(s)"
  local active="" prev=""
  [ -L "$CURRENT_LINK" ] && active="$(readlink "$CURRENT_LINK")"
  [ -f "$PREV_FILE" ] && prev="$(cat "$PREV_FILE" 2>/dev/null || true)"
  # Newest-first list of release dirs.
  local dirs=()
  local d
  while IFS= read -r d; do
    [ -n "$d" ] && dirs+=("$d")
  done < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -name 'v*' \
             -exec stat -f '%m %N' {} + 2>/dev/null | sort -rn | awk '{print $2}')
  local kept=0
  for d in "${dirs[@]}"; do
    if [ "$d" = "$active" ] || [ "$d" = "$prev" ]; then
      continue
    fi
    kept=$((kept + 1))
    if [ "$kept" -ge "$KEEP_RELEASES" ]; then
      # `find` output is not trusted as a deletion target. Resolve and prove
      # its parent immediately before rm so a traversal/symlink surprise
      # cannot turn pruning into a live-state delete.
      assert_release_target "$d"
      log "prune: removing old release $d"
      run rm -rf "$d"
    fi
  done
}

# The focused shell harness sources only the helpers above. This is deliberately
# an opt-in no-op for a production invocation: it cannot cause a release cut.
if [ "${MINI_RELEASE_CUT_TEST_LIB:-0}" = "1" ]; then
  if [ "${BASH_SOURCE[0]}" != "$0" ]; then
    return 0
  fi
  exit 0
fi

# ===========================================================================
# MODE: rollback
# ===========================================================================
[ -d "$RELEASES_DIR" ] || die "releases dir missing: $RELEASES_DIR"
RELEASES_DIR="$(canonical_existing_dir "$RELEASES_DIR")" \
  || die "could not canonicalize releases dir: $RELEASES_DIR"
PREV_FILE="$RELEASES_DIR/.previous"
CUT_LOCK_DIR="$RELEASES_DIR/.mini-release-cut.lock"
LAST_RECEIPT_FILE="$RELEASES_DIR/.mini-release-last-receipt.json"
REFRESH_BACKUP_FILE="$RELEASES_DIR/.clickup_workspace_refresh.previous"

if [ "$PREFLIGHT" -eq 1 ] && {
  [ "$DRY_RUN" -eq 1 ] || [ "$DO_ROLLBACK" -eq 1 ] \
    || [ "$DO_PRUNE" -eq 1 ] || [ "$OFFLINE" -eq 1 ]
}; then
  die "--preflight cannot be combined with --dry-run, --rollback, --prune, or --offline"
fi

# This trap owns both failure cleanup and lock release. NEW_DIR remains empty
# for an explicit rollback, so that mode only releases its lock.
NEW_DIR=""
# shellcheck disable=SC2329 # registered as an EXIT trap immediately below
cleanup_on_exit() {
  local status=$?
  if [ "$status" -ne 0 ] && [ "$DRY_RUN" -ne 1 ] && [ -n "$NEW_DIR" ] && [ -e "$NEW_DIR" ]; then
    local live=""
    [ -L "$CURRENT_LINK" ] && live="$(readlink "$CURRENT_LINK")"
    if [ "$live" != "$NEW_DIR" ]; then
      # Prove the resolved parent immediately before removal, even on an
      # error path where values may have been partially initialized.
      assert_release_target "$NEW_DIR"
      warn "cleanup: removing partially-built release dir: $NEW_DIR"
      rm -rf "$NEW_DIR"
    fi
  fi
  release_cut_lock
  trap - EXIT
  exit "$status"
}

acquire_cut_lock
trap cleanup_on_exit EXIT

if [ "$DO_ROLLBACK" -eq 1 ]; then
  [ -L "$CURRENT_LINK" ] || die "no runtime-current symlink at $CURRENT_LINK"
  rollback_to_previous "explicit --rollback"
  exit 0
fi

# ===========================================================================
# MODE: prune-only (no --ref build requested implicitly still resolves a ref,
# so treat --prune WITHOUT intending a build by running the cut; but if the
# operator ONLY wants a prune they can combine with a normal cut). Prune runs
# at the end of a successful cut. A standalone prune is available here:
# `--prune` with the current release already active still does a full cut of
# the default ref. To prune WITHOUT a cut, this branch is intentionally not
# offered — prune only ever runs after a verified-healthy cut, so it can
# never orphan the active release.
# ===========================================================================

# ===========================================================================
# MODE: cut a new release
# ===========================================================================
[ -L "$CURRENT_LINK" ] || die "no runtime-current symlink at $CURRENT_LINK — refusing to bootstrap a layout from scratch"
command -v git  >/dev/null || die "git not found"
if [ "$PREFLIGHT" -ne 1 ]; then
  command -v npm  >/dev/null || die "npm not found on PATH (expected /opt/homebrew/bin)"
  command -v uv   >/dev/null || warn "uv not found — will fall back to python venv+pip if needed"
fi

log "fetching origin in current release clone: $CURRENT_LINK"
# Disable background maintenance (auto-gc/repack) for this fetch: a
# maintenance job detached by the fetch can race the local clone below and
# produce transient "unable to read sha1 file" errors.
run git -c gc.auto=0 -c maintenance.auto=false -C "$CURRENT_LINK" fetch --prune origin

ORIGIN_URL="$(git_current remote get-url origin)"
log "origin: $ORIGIN_URL"

# Resolve the target commit. Accept a branch (origin/<ref>) or a raw sha.
SHA=""
if SHA="$(git_current rev-parse --verify --quiet "origin/${REF}^{commit}" 2>/dev/null)" && [ -n "$SHA" ]; then
  log "resolved ref '$REF' → origin/$REF → $SHA"
elif SHA="$(git_current rev-parse --verify --quiet "${REF}^{commit}" 2>/dev/null)" && [ -n "$SHA" ]; then
  log "resolved ref '$REF' → $SHA"
else
  die "could not resolve ref '$REF' to a commit (tried origin/$REF and $REF)"
fi
SHORT_SHA="${SHA:0:12}"

# Polling mode is evaluated under the same lock as the eventual cut, after the
# fetch and immutable commit resolution. This closes the check-then-cut race.
ACTIVE_SHA="$(git_current rev-parse --verify "HEAD^{commit}")" \
  || die "could not resolve active runtime commit"
if [ "$IF_ADVANCED" -eq 1 ]; then
  ADVANCEMENT="$(classify_ref_advancement "$ACTIVE_SHA" "$SHA")"
  ACTIVE_TARGET="$(readlink "$CURRENT_LINK")"
  ACTIVE_REFRESH_SOURCE_HASH=""
  [ -f "$CURRENT_LINK/$VENDORED_REFRESH_REL" ] \
    && ACTIVE_REFRESH_SOURCE_HASH="$(sha256_file "$CURRENT_LINK/$VENDORED_REFRESH_REL")"
  ACTIVE_REFRESH_DEPLOYED_HASH=""
  [ -f "$DEPLOYED_REFRESH" ] \
    && ACTIVE_REFRESH_DEPLOYED_HASH="$(sha256_file "$DEPLOYED_REFRESH")"
  case "$ADVANCEMENT" in
    equal)
      if [ "$PREFLIGHT" -eq 1 ]; then
        ok "release preflight passed: $REF already active at $SHA"
        exit 0
      fi
      write_release_receipt "noop" "$ACTIVE_SHA" "$SHA" "$ACTIVE_TARGET" \
        "$ACTIVE_REFRESH_SOURCE_HASH" "$ACTIVE_REFRESH_DEPLOYED_HASH" \
        "resolved ref already active" \
        || die "could not record no-op release receipt"
      ok "release poll no-op: $REF already active at $SHA"
      exit 0
      ;;
    advance)
      ok "validated strict descendant advance: $ACTIVE_SHA -> $SHA"
      if [ "$PREFLIGHT" -eq 1 ]; then
        ok "release preflight passed: $REF is a strict descendant"
        exit 0
      fi
      ;;
    behind|diverged)
      if [ "$PREFLIGHT" -ne 1 ]; then
        write_release_receipt "rejected" "$ACTIVE_SHA" "$SHA" "$ACTIVE_TARGET" \
          "$ACTIVE_REFRESH_SOURCE_HASH" "$ACTIVE_REFRESH_DEPLOYED_HASH" \
          "resolved ref is $ADVANCEMENT relative to active runtime" \
          || die "could not record rejected release receipt"
      fi
      die "ref '$REF' is $ADVANCEMENT relative to active runtime; refusing non-descendant release"
      ;;
    *)
      die "unexpected advancement classification: $ADVANCEMENT"
      ;;
  esac
fi

# Derive version from pyproject.toml AT THE TARGET REF (not the working tree).
VERSION="$(git_current show "${SHA}:pyproject.toml" 2>/dev/null \
             | grep -m1 -E '^version = "' | sed -E 's/^version = "([^"]+)".*/\1/')"
[ -n "$VERSION" ] || die "could not read [project] version from pyproject.toml at $SHA"
valid_release_version "$VERSION" \
  || die "invalid project version (must be an ASCII PEP 440-safe path component): $VERSION"
log "version at target ref: $VERSION"

NEW_DIR="$(release_target "v${VERSION}-${SHORT_SHA}")"
assert_release_target "$NEW_DIR"
assert_not_forbidden "$NEW_DIR"

# HARD SAFETY INVARIANT: never mutate an existing release in place.
{ [ -e "$NEW_DIR" ] || [ -L "$NEW_DIR" ]; } \
  && die "target release dir already exists: $NEW_DIR (refusing in-place mutation)"

log "new release dir: $NEW_DIR"

# --- Build ENTIRELY in the new dir before any switch -----------------------

# Network clone (default): clone straight from the real origin URL over the
# network, full (no --filter). runtime-current is a blobless partial clone
# (remote.origin.partialclonefilter=blob:none) — a *local-path* clone from it
# only copies whatever blobs happen to already be present in its object
# store, and the resulting clone has no promisor remote configured to fetch
# the rest on demand. That is what produced the "unable to read sha1 file" /
# silently-deleted-files failures in cut attempts 1-2: `checkout --detach`
# can exit 0 while dropping files whose blobs were never locally cached.
# Cloning from $ORIGIN_URL instead always yields a complete object set.
#
# --offline opts into the old local-path behavior (network origin
# unreachable). It is NOT relied on for correctness — the post-checkout
# integrity check below (git status/diff + a spot-check file) catches any
# gap from either path and fails loudly instead of shipping a silently
# corrupt release.
#
# Retry once on failure: git background maintenance (auto-gc/repack) detached
# by the fetch above can race a same-second clone/checkout and produce
# transient "unable to read sha1 file" errors. On failure, blow away the
# partial dir, wait for maintenance to settle, and redo the whole sequence;
# a second failure aborts.
clone_and_checkout() {
  local src="$ORIGIN_URL" desc="network"
  if [ "$OFFLINE" -eq 1 ]; then
    src="$CURRENT_LINK"
    desc="local (--offline)"
  fi

  log "clone ($desc): git clone --no-checkout $src $NEW_DIR"
  # git clone creates NEW_DIR: prove its canonical parent immediately first.
  assert_release_target "$NEW_DIR"
  run git clone --no-checkout "$src" "$NEW_DIR" || return 1

  if [ "$OFFLINE" -eq 1 ]; then
    run git -C "$NEW_DIR" remote set-url origin "$ORIGIN_URL" || return 1
  fi

  # Defensive: never let a blobless partial-clone filter leak into the new
  # release regardless of source — a filtered clone can silently drop files
  # during checkout, which is the exact root cause being fixed here.
  if [ "$DRY_RUN" -ne 1 ]; then
    git -C "$NEW_DIR" config --unset-all remote.origin.partialclonefilter 2>/dev/null || true
  fi

  log "checkout $SHA (detached)"
  run git -C "$NEW_DIR" checkout --detach "$SHA" || return 1

  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi

  # Never trust checkout's exit code alone — it has been observed to exit 0
  # while silently deleting files when object data is missing. Verify tree
  # integrity explicitly before this release dir is allowed to go live.
  local dirty
  dirty="$(git -C "$NEW_DIR" status --porcelain)"
  if [ -n "$dirty" ]; then
    warn "post-checkout tree is dirty (possible silent corruption):"
    printf '%s\n' "$dirty" >&2
    return 1
  fi
  if ! git -C "$NEW_DIR" diff --quiet HEAD; then
    warn "post-checkout diff vs HEAD is non-empty (possible silent corruption)"
    return 1
  fi
  if [ ! -f "$NEW_DIR/hermes_cli/config.py" ]; then
    warn "post-checkout spot-check failed: hermes_cli/config.py missing"
    return 1
  fi
  ok "post-checkout tree integrity verified (clean status, diff matches HEAD, config.py present)"
}

if ! clone_and_checkout; then
  warn "clone/checkout failed (possible git maintenance race, or a genuine object gap) — retrying once"
  # Prove the resolved parent immediately before deleting the failed clone.
  assert_release_target "$NEW_DIR"
  run rm -rf "$NEW_DIR"
  sleep 5
  clone_and_checkout \
    || die "clone/checkout failed twice for $SHA — aborting (possible non-transient git maintenance/object race, or missing objects at origin)"
fi

# --- Build the Python venv inside the release dir --------------------------
# The repo's own setup-hermes.sh prefers a hash-verified `uv sync --extra all
# --locked` into UV_PROJECT_ENVIRONMENT; fall back to uv venv + editable pip
# install, matching setup-hermes.sh's tiers. (Design choice: uv sync is the
# primary path because uv.lock is present and gives hash-verified transitives;
# see pyproject.toml's supply-chain rationale.)
log "building venv in $NEW_DIR/venv"
if command -v uv >/dev/null; then
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m (cd %s && UV_PROJECT_ENVIRONMENT=%s/venv uv sync --extra all --locked)\n' "$NEW_DIR" "$NEW_DIR"
  else
    if ! ( cd "$NEW_DIR" && UV_PROJECT_ENVIRONMENT="$NEW_DIR/venv" uv sync --extra all --locked ); then
      warn "uv sync --locked failed; falling back to uv venv + editable pip install"
      ( cd "$NEW_DIR" && uv venv "$NEW_DIR/venv" \
          && VIRTUAL_ENV="$NEW_DIR/venv" uv pip install -e ".[all]" ) \
        || die "venv build failed"
    fi
  fi
else
  # uv is unavailable: DO NOT fall through to bare `python3` — on the mini
  # that resolves to Homebrew's python 3.14, which violates this repo's
  # `<3.14,>=3.11` pin (pyproject.toml). Probe explicitly compatible
  # interpreters (checking both PATH and Homebrew's bin directly, since a
  # non-interactive ssh PATH may omit /opt/homebrew/bin) and abort if none
  # are present rather than silently building an incompatible venv.
  FALLBACK_PYTHON=""
  for cand in python3.13 python3.12 python3.11; do
    for bin in "$cand" "/opt/homebrew/bin/$cand"; do
      if command -v "$bin" >/dev/null 2>&1; then
        FALLBACK_PYTHON="$(command -v "$bin")"
        break 2
      fi
    done
  done
  [ -n "$FALLBACK_PYTHON" ] \
    || die "uv not found and no compatible python interpreter found (tried python3.13/python3.12/python3.11 on PATH and in /opt/homebrew/bin) — refusing to fall back to bare python3"
  log "uv unavailable; using fallback interpreter: $FALLBACK_PYTHON"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '\033[35m[DRY-RUN]\033[0m (cd %s && %s -m venv venv && venv/bin/pip install -e ".[all]")\n' "$NEW_DIR" "$FALLBACK_PYTHON"
  else
    ( cd "$NEW_DIR" && "$FALLBACK_PYTHON" -m venv venv \
        && "$NEW_DIR/venv/bin/pip" install --upgrade pip \
        && "$NEW_DIR/venv/bin/pip" install -e ".[all]" ) || die "venv build failed"
  fi
fi

# --- Build the web dashboard bundle into hermes_cli/web_dist ---------------
# vite is configured with outDir ../hermes_cli/web_dist (web/vite.config.ts).
log "building web dist (npm install && npm run build --workspace web)"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '\033[35m[DRY-RUN]\033[0m (cd %s && npm install && npm run build --workspace web)\n' "$NEW_DIR"
else
  ( cd "$NEW_DIR" && npm install && npm run build --workspace web ) || die "web build failed"
fi

# --- Verify the build BEFORE any switch ------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  printf '\033[35m[DRY-RUN]\033[0m verify: venv python imports hermes_cli.main + web_dist/index.html + governed refresh source compile\n'
else
  log "verifying build artifacts"
  ( cd "$NEW_DIR" && "$NEW_DIR/venv/bin/python" -c "import hermes_cli.main" ) \
    || die "build verify failed: venv python cannot import hermes_cli.main"
  [ -f "$NEW_DIR/hermes_cli/web_dist/index.html" ] \
    || die "build verify failed: missing $NEW_DIR/hermes_cli/web_dist/index.html"
  [ -f "$NEW_DIR/$VENDORED_REFRESH_REL" ] && [ ! -L "$NEW_DIR/$VENDORED_REFRESH_REL" ] \
    || die "build verify failed: governed refresh source missing or symlinked"
  if ! "$NEW_DIR/venv/bin/python" - "$NEW_DIR/$VENDORED_REFRESH_REL" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
compile(source, sys.argv[1], "exec")
PY
  then
    die "build verify failed: governed refresh source does not compile"
  fi
  ok "build verified (import OK, web dist present, governed refresh compiles)"
fi

# Preserve the exact pre-cut deployed script for bootstrap rollback before
# runtime-current or any protected operational file changes.
stage_refresh_backup

# --- Record previous target for rollback (lives under releases/, allowed) --
PREV_TARGET="$(readlink "$CURRENT_LINK")"
assert_release_target "$PREV_TARGET"
log "recording previous release for rollback: $PREV_TARGET → $PREV_FILE"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '\033[35m[DRY-RUN]\033[0m printf %%s %s > %s\n' "$PREV_TARGET" "$PREV_FILE"
else
  # .previous is a release-owned file; never let a malformed path escape it.
  assert_regular_release_file "$PREV_FILE"
  printf '%s\n' "$PREV_TARGET" > "$PREV_FILE"
fi

# --- Switch: atomic symlink swap + restart + verify ------------------------
GW_OFFSET="$(log_offset)"
repoint_symlink "$NEW_DIR"
kickstart_after_switch "$GATEWAY_TARGET" "gateway"

if ! verify_gateway "$NEW_DIR" "$GW_OFFSET"; then
  warn "gateway did not verify healthy on new release — rolling back"
  rollback_to_previous "gateway verify failed"
  die "cut aborted and rolled back to previous release"
fi

kickstart_after_switch "$DASHBOARD_TARGET" "dashboard"
if ! verify_dashboard; then
  warn "dashboard did not verify healthy on new release — rolling back"
  rollback_to_previous "dashboard verify failed"
  die "cut aborted and rolled back to previous release"
fi

if ! install_governed_refresh "$NEW_DIR"; then
  warn "governed ClickUp refresh install failed — rolling back"
  rollback_to_previous "governed refresh install failed"
  die "cut aborted and rolled back to previous release"
fi

if ! install_clickup_cli "$NEW_DIR"; then
  warn "managed ClickUp CLI install failed — rolling back"
  rollback_to_previous "managed ClickUp CLI install failed"
  die "cut aborted and rolled back to previous release"
fi

REFRESH_SOURCE_HASH="$(sha256_file "$NEW_DIR/$VENDORED_REFRESH_REL" 2>/dev/null || true)"
REFRESH_DEPLOYED_HASH="$(sha256_file "$DEPLOYED_REFRESH" 2>/dev/null || true)"
if [ -z "$REFRESH_SOURCE_HASH" ] || [ "$REFRESH_SOURCE_HASH" != "$REFRESH_DEPLOYED_HASH" ]; then
  warn "governed refresh source/deployed hash verification failed — rolling back"
  rollback_to_previous "governed refresh hash verification failed"
  die "cut aborted and rolled back to previous release"
fi
RECEIPT_EVENT="cut"
[ "$IF_ADVANCED" -eq 1 ] && RECEIPT_EVENT="advanced"
record_cut_receipt_or_rollback "$RECEIPT_EVENT" "$ACTIVE_SHA" "$SHA" "$NEW_DIR" \
  "$REFRESH_SOURCE_HASH" "$REFRESH_DEPLOYED_HASH" \
  "release cut and governed script deployment verified"

ok "release cut complete: runtime-current → $NEW_DIR (v${VERSION}-${SHORT_SHA})"

# --- Optional prune (explicit only) ----------------------------------------
if [ "$DO_PRUNE" -eq 1 ]; then
  prune_releases
fi

exit 0
