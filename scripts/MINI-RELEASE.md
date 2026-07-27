# Mac mini release cut — `scripts/mini-release-cut.sh`

Safe, repeatable release cut for the Hermes production Mac mini.

## Why this exists

On **2026-07-19** an improvised cutover to a
`~/.hermes/releases/<ver>-<sha>/` + `~/.hermes/runtime-current` symlink layout
destroyed runtime state: SQLite DBs were truncated under live WAL connections,
and `config.yaml`, the auth token, and LaunchAgents were deleted. No committed
automation produced that layout, so it could not be reviewed or reproduced.

This script **is** that automation. It builds a brand-new release directory in
full, verifies it, and only then atomically repoints the `runtime-current`
symlink and restarts the services. Persistent runtime state remains untouched
except for the two explicit, rollback-safe governed deployments described
below: `clickup_workspace_refresh.py` and the canonical launchd environment.

Tracked in ClickUp `86e2ddah5`.

## Layout on the mini

- Releases live at `~/.hermes/releases/v<version>-<12charsha>/` (a git clone +
  its own `venv/` + built `hermes_cli/web_dist/`).
- The active release is the `~/.hermes/runtime-current` symlink.
- `~/.hermes/releases/.previous` records the prior symlink target for rollback.
- Gateway: launchd `gui/501/ai.hermes.gateway` (API on `:8642`).
- Dashboard: launchd `gui/501/com.colingreig.hermes-dashboard` (`:9119`).
- `~/.local/bin/cu-clickup` is a release-managed, stable wrapper around the
  protected live `~/.hermes/scripts/clickup_workspace_refresh.py`;
  `/opt/homebrew/bin/cu-clickup` links to it so clean non-interactive shells
  can discover the command without depending on dotfile PATH setup.
- The canonical refresh source is
  `machine-setup/mini-scripts/clickup_workspace_refresh.py`. A successful cut
  atomically installs those exact bytes at the protected live path and records
  both SHA-256 values in a content-addressed receipt.
- The canonical launchd environment lives in
  `machine-setup/mini-scripts/` and is installed by
  `reconcile_launchd_environment.py`: source-identical wrappers/minter/resolver,
  a reference-only secret file, and generated gateway/dashboard plists. Its
  content-addressed snapshot restores exact prior bytes on rollback.
- `.mini-release-last-receipt.json` is the stable latest receipt; immutable
  `.mini-release-receipt-<sha256>.json` siblings are addressed by their exact
  payload bytes.

## Usage

Run **on the mini** (over `ssh mini`). `node`/`npm` live in `/opt/homebrew/bin`,
which is not on a non-interactive ssh PATH — the script extends PATH itself.

```bash
# Standard cut of prod-live-patches:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches

# Preview every mutating action, change nothing:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --dry-run

# Polling-safe mode: equal is a successful no-op; only a strict descendant cuts:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --if-advanced

# Cut a specific sha or branch:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref <sha-or-branch>

# Roll back to the previous release (no build):
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback

# Cut, then prune releases older than the newest 3:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --prune
```

`--ref` defaults to `prod-live-patches`.

## Optional local polling job

The poller is local-only and does not expose a webhook. Its wrapper delegates
all decisions to the locked cutter:

```bash
~/.hermes/runtime-current/scripts/install-mini-release-poller.sh --install
# Review the installed plist, then explicitly load it:
~/.hermes/runtime-current/scripts/install-mini-release-poller.sh --install-and-enable
```

The installer first runs the cutter's dedicated `--preflight` mode. Preflight
acquires the real release lock, fetches current origin metadata, and refuses a
behind or diverged branch without building, switching, restarting services, or
writing a receipt. The LaunchAgent runs every 15 minutes and is not `RunAtLoad`.
Do not enable it until `prod-live-patches` contains the active runtime commit:
on 2026-07-26 the Mini's active `231607384a1d` and branch `9a48716a786d` were
diverged, so bootstrap is intentionally blocked until the branch is reconciled.

## What a cut does (order matters)

1. `git -C ~/.hermes/runtime-current fetch --prune origin` — fetch in the
   **current** clone (no new dir yet).
2. Resolve the target commit (`origin/<ref>` or a raw sha) and read the
   `[project]` version from `pyproject.toml` **at that commit**.
3. Name the new dir `releases/v<version>-<12charsha>`. **Refuse if it already
   exists** (no in-place mutation).
4. Build **entirely in the new dir**: full network clone from the real
   `origin` URL (the default; it avoids inheriting missing blobs from the
   blobless `runtime-current` clone), detached-checkout the sha, build the venv
   (`uv sync --extra all --locked`, falling back to `uv venv` + editable pip),
   build the web dist (`npm install && npm run build --workspace web` →
   `hermes_cli/web_dist/`). `--offline` is the explicit, best-effort local
   clone fallback; its integrity check must pass before it can be activated.
5. **Verify the build before any switch**: `venv/bin/python -c "import
   hermes_cli.main"`, `hermes_cli/web_dist/index.html` present, and the governed
   refresh and launchd Python/shell sources compile.
6. Back up the exact currently deployed refresh bytes under `releases/`, then
   record the current symlink target to `releases/.previous`.
7. **Atomic switch**: `ln -sfn` a temp symlink + `mv -fh` over
   `runtime-current`.
8. Transactionally reconcile both LaunchAgents and their canonical wrappers,
   then reload with `bootout` + `bootstrap`. Plists point only to wrappers;
   permanent auth parks cleanly while transient exhaustion remains retryable.
9. **Verify (up to 60s)**: gateway process running through the new release,
   `Gateway running with N platform(s)` with N ≥ 2 in `gateway.log`, and
   `:8642` listening; then verify the dashboard (`:9119` → HTTP 200).
10. Atomically install and hash-verify the governed refresh source at
   `~/.hermes/scripts/clickup_workspace_refresh.py`, then install (or repair)
   the executable `~/.local/bin/cu-clickup` wrapper and PATH link.
11. Record a deterministic receipt with old/new commits, runtime target, source
    hash, deployed hash, ref, event, and result detail.
12. On **any** verification, governed install, CLI install, hash, or receipt
    failure: **automatic rollback** of the runtime target, launchd snapshot, and
    governed refresh bytes, restart, re-verify, and exit non-zero.

## Hard safety invariants (enforced in code, not comments)

1. The build only ever writes **under `~/.hermes/releases/`**. Each release
   target is reconstructed from a canonical `releases/` parent, and immediately
   before every create or removal that resolved parent must equal `releases/`.
   Version strings must be ASCII PEP 440-safe components: they begin with a
   decimal digit and may contain only letters, digits, `.`, `!`, `+`, `_`, and
   `-`; whitespace, controls, slashes, shell punctuation, and option-looking
   values are rejected.
2. Before a cut, `git fetch --prune origin` deliberately updates **Git metadata
   only** in the existing `runtime-current` clone; this is the one operational
   write outside `releases/` needed to resolve the requested ref. It never
   changes that clone's checked-out worktree or live runtime state. The other
   out-of-`releases/` actions are (a) the atomic `runtime-current` symlink
   repoint, (b) the `launchctl` restart, and (c) the atomic replacement of the
   managed command `~/.local/bin/cu-clickup` plus its
   `/opt/homebrew/bin/cu-clickup` discovery link, each funnelled through
   dedicated functions. The governed operational replacements are
   exact-path-only, refuse symlink source/destination paths, stage and hash
   in the destination directory, and atomically rename over only the declared
   refresh/launchd assets. The ClickUp wrapper contains no token and reads
   credentials from the environment only when it invokes that script.
3. It **never** touches `~/.hermes/{config.yaml,*.db,cron/,logs/,recovery/}` or
   `~/.config`. `~/.hermes/scripts/` and `~/Library/LaunchAgents` remain
   forbidden to generic writes; only the exact governed refresh and launchd
   reconciler target sets are exceptions. Logs remain read-only.
4. It **refuses to run** if the target release dir already exists — never
   mutates a release in place.
5. It **refuses to bootstrap** a missing `runtime-current` symlink or
   `releases/` dir from scratch (that improvisation is what caused the incident).
6. The symlink swap is **atomic** (`ln -sfn` temp + `mv -fh` rename). `-h`
   prevents BSD `mv` from following an existing symlink-to-directory.
7. `.previous` (under `releases/`) records the rollback target; failed
   verification auto-rolls-back to it.
8. Pruning keeps the newest **3** releases and **only runs on explicit
   `--prune`** — never by default, and never removes the active or previous
   release.
9. `--dry-run` prints every mutating action and performs none.
10. A `releases/.mini-release-cut.lock` directory is acquired atomically for
    the full cut, rollback, or prune operation, so concurrent operators cannot
    race a switch or cleanup.
11. `--if-advanced` resolves and classifies the ref while holding that same
    lock. Equal commits emit a content-addressed no-op receipt. Only a strict
    descendant can cut; behind, diverged, or unresolvable ancestry fails closed.
12. Receipt filenames are the SHA-256 of their canonical JSON payload. Repeated
    polls in identical state reuse the same immutable receipt.

## Rollback

```bash
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback
```

Repoints `runtime-current` to the release recorded in `releases/.previous`,
atomically restores that release's governed refresh source (or the staged
pre-vendor bytes for the bootstrap cut), restores the exact prior launchd
snapshot and managed CLI, restarts both services, and re-verifies. No build. If
restoration or either service does not verify healthy it exits non-zero and
asks for manual intervention rather than looping.
