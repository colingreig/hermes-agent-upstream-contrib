#!/usr/bin/env bash
# Two-phase Hermes engine upgrade flow: rebase+test in the dev clone, then a
# gated cutover of the live prod checkout. Replaces ad hoc `hermes update` on
# prod, which defaults to branch "main" and will silently switch prod OFF
# prod-live-patches (see brain: learnings/2026-07-04-hermes-update-defaults-to-main-branch-landmine).
#
# Usage:
#   hermes-engine-upgrade.sh rebase              # safe, run anytime, in ~/dev/hermes-agent
#   hermes-engine-upgrade.sh cutover              # dry-run: shows the plan, changes nothing
#   hermes-engine-upgrade.sh cutover --yes        # applies the cutover + restarts the gateway
set -euo pipefail

DEV_REPO="$HOME/dev/hermes-agent"
PROD_REPO="$HOME/.hermes/hermes-agent"
BRANCH="prod-live-patches"
GW_PLIST="$HOME/Library/LaunchAgents/ai.hermes.gateway.plist"

cmd_rebase() {
  cd "$DEV_REPO"
  echo "→ fetching origin + upstream..."
  git fetch origin --quiet
  if ! git fetch upstream --quiet; then
    echo "✗ git fetch upstream failed (network/auth/remote issue) — main may be stale. Aborting." >&2
    exit 1
  fi

  echo "→ syncing fork main with upstream..."
  git checkout main --quiet
  git merge --ff-only upstream/main --quiet 2>/dev/null || echo "  (main already current with upstream, or fork-main has diverged — check manually)"
  git push origin main --quiet

  echo "→ rebasing $BRANCH onto main..."
  git branch -f "$BRANCH" "origin/$BRANCH" --quiet 2>/dev/null || true
  git checkout "$BRANCH" --quiet
  if ! git rebase main; then
    echo "✗ Rebase hit conflicts. Resolve manually, then:"
    echo "    git add <files> && git rebase --continue"
    echo "  Verify with: git diff origin/$BRANCH^..origin/$BRANCH -- <file> vs git diff HEAD^..HEAD -- <file>"
    echo "  (the +/- content lines should match except for any intentional resolution)"
    exit 1
  fi

  echo "→ syntax-checking every changed Python file vs main..."
  if ! changed_py=$(git diff --name-only origin/main..HEAD -- '*.py'); then
    echo "✗ Failed to list changed Python files (git diff failed) — cannot verify syntax, aborting." >&2
    exit 1
  fi
  bad=0
  for f in $changed_py; do
    python3 -m py_compile "$f" || bad=1
  done
  if [ "$bad" -ne 0 ]; then
    echo "✗ Syntax check failed — do NOT push. Fix and re-run."
    exit 1
  fi
  echo "✓ Syntax OK."

  echo "→ pushing rebased $BRANCH to fork (force-with-lease)..."
  git push origin "$BRANCH" --force-with-lease

  echo "✓ Rebase complete and pushed. Next: hermes-engine-upgrade.sh cutover --yes (on the mini, when ready to restart the live gateway)."
}

cmd_cutover() {
  local apply="${1:-}"
  cd "$PROD_REPO"

  # Remote naming is NOT consistent between the two checkouts: the dev clone
  # calls the fork "origin" and upstream "upstream"; prod (as of 2026-07-04)
  # calls the fork "fork" and upstream "origin". Resolve by URL, not by name.
  fork_remote=$(git remote -v | awk '/colingreig\/hermes-agent/ && /fetch/ {print $1; exit}')
  if [ -z "$fork_remote" ]; then
    echo "✗ Could not find a remote pointing at colingreig/hermes-agent. Check 'git remote -v'."
    exit 1
  fi

  current_branch=$(git rev-parse --abbrev-ref HEAD)
  if [ "$current_branch" != "$BRANCH" ]; then
    echo "✗ Prod is on '$current_branch', not '$BRANCH'. Investigate before touching it."
    exit 1
  fi

  dirty=$(git status --porcelain --untracked-files=no)
  if [ -n "$dirty" ]; then
    echo "✗ Prod has uncommitted tracked changes — never reset with these present:"
    echo "$dirty"
    exit 1
  fi

  git fetch "$fork_remote" --quiet
  local_sha=$(git rev-parse HEAD)
  remote_sha=$(git rev-parse "$fork_remote/$BRANCH")

  if ! git merge-base --is-ancestor "$local_sha" "$remote_sha"; then
    echo "✗ Prod HEAD ($local_sha) is not an ancestor of $fork_remote/$BRANCH ($remote_sha)."
    echo "  A hard reset would silently discard commits that exist only on prod's local HEAD:"
    git log --oneline "$remote_sha..$local_sha" | sed 's/^/    - /'
    echo "  If these were committed directly on prod (against convention) without pushing, push/cherry-pick"
    echo "  them to $fork_remote first, or confirm they're safe to discard (recoverable via 'git reflog' until the next gc)."
    exit 1
  fi

  if [ "$local_sha" == "$remote_sha" ]; then
    echo "✓ Prod already at $fork_remote/$BRANCH ($local_sha). Nothing to do."
    exit 0
  fi

  echo "Plan: reset $PROD_REPO from $local_sha to $remote_sha ($fork_remote/$BRANCH)."
  git log --oneline "$local_sha..$remote_sha" | sed 's/^/  + /'

  if [ "$apply" != "--yes" ]; then
    echo
    echo "Dry run only — nothing changed. Re-run with --yes to apply + restart the gateway."
    echo "This stops the live Hermes gateway for a few seconds; confirm no in-flight task depends on it first."
    exit 0
  fi

  echo "→ resetting prod to $fork_remote/$BRANCH..."
  git reset --hard "$fork_remote/$BRANCH"

  echo "→ clearing stale __pycache__..."
  find . -name '__pycache__' -type d -prune -exec rm -rf {} +

  echo "→ restarting gateway (launchctl kickstart)..."
  uid_num=$(id -u)
  launchctl kickstart -k "gui/$uid_num/ai.hermes.gateway"

  echo "✓ Cutover complete. Verify: tail -f ~/.hermes/logs/*.log and confirm the gateway comes back healthy."
}

case "${1:-}" in
  rebase) cmd_rebase ;;
  cutover) cmd_cutover "${2:-}" ;;
  *)
    echo "Usage: $0 {rebase|cutover [--yes]}"
    exit 1
    ;;
esac
