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
# live runtime state (DBs, config, cron, scripts, logs, LaunchAgents).
#
# Tracked in ClickUp 86e2ddah5.
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
GATEWAY_LOG="$HERMES_HOME/logs/gateway.log"

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

usage() {
  cat <<'EOF'
Usage: mini-release-cut.sh [--ref <branch-or-sha>] [--rollback] [--prune] [--dry-run] [--offline]

  --ref <ref>   Branch or sha to cut (default: prod-live-patches).
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
  kickstart "$GATEWAY_TARGET"
  if verify_gateway "$prev" "$offset"; then
    kickstart "$DASHBOARD_TARGET"
    verify_dashboard || die "rollback dashboard did NOT verify healthy — MANUAL INTERVENTION REQUIRED (release: $prev)"
    ok "rollback complete → $prev"
    return 0
  fi
  die "rollback restart did NOT verify healthy — MANUAL INTERVENTION REQUIRED (release: $prev)"
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
command -v npm  >/dev/null || die "npm not found on PATH (expected /opt/homebrew/bin)"
command -v uv   >/dev/null || warn "uv not found — will fall back to python venv+pip if needed"

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
  printf '\033[35m[DRY-RUN]\033[0m verify: venv python imports hermes_cli.main + web_dist/index.html present\n'
else
  log "verifying build artifacts"
  ( cd "$NEW_DIR" && "$NEW_DIR/venv/bin/python" -c "import hermes_cli.main" ) \
    || die "build verify failed: venv python cannot import hermes_cli.main"
  [ -f "$NEW_DIR/hermes_cli/web_dist/index.html" ] \
    || die "build verify failed: missing $NEW_DIR/hermes_cli/web_dist/index.html"
  ok "build verified (import OK, web_dist/index.html present)"
fi

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
kickstart "$GATEWAY_TARGET"

if ! verify_gateway "$NEW_DIR" "$GW_OFFSET"; then
  warn "gateway did not verify healthy on new release — rolling back"
  rollback_to_previous "gateway verify failed"
  die "cut aborted and rolled back to previous release"
fi

kickstart "$DASHBOARD_TARGET"
if ! verify_dashboard; then
  warn "dashboard did not verify healthy on new release — rolling back"
  rollback_to_previous "dashboard verify failed"
  die "cut aborted and rolled back to previous release"
fi

ok "release cut complete: runtime-current → $NEW_DIR (v${VERSION}-${SHORT_SHA})"

# --- Optional prune (explicit only) ----------------------------------------
if [ "$DO_PRUNE" -eq 1 ]; then
  prune_releases
fi

exit 0
