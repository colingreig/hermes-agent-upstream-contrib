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
symlink and restarts the services. It never mutates live runtime state.

Tracked in ClickUp `86e2ddah5`.

## Layout on the mini

- Releases live at `~/.hermes/releases/v<version>-<12charsha>/` (a git clone +
  its own `venv/` + built `hermes_cli/web_dist/`).
- The active release is the `~/.hermes/runtime-current` symlink.
- `~/.hermes/releases/.previous` records the prior symlink target for rollback.
- Gateway: launchd `gui/501/ai.hermes.gateway` (API on `:8642`).
- Dashboard: launchd `gui/501/com.colingreig.hermes-dashboard` (`:9119`).

## Usage

Run **on the mini** (over `ssh mini`). `node`/`npm` live in `/opt/homebrew/bin`,
which is not on a non-interactive ssh PATH — the script extends PATH itself.

```bash
# Standard cut of prod-live-patches:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches

# Preview every mutating action, change nothing:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --dry-run

# Cut a specific sha or branch:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref <sha-or-branch>

# Roll back to the previous release (no build):
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback

# Cut, then prune releases older than the newest 3:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --prune
```

`--ref` defaults to `prod-live-patches`.

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
   hermes_cli.main"` and `hermes_cli/web_dist/index.html` present.
6. Record the current symlink target to `releases/.previous`.
7. **Atomic switch**: `ln -sfn` a temp symlink + `mv -fh` over
   `runtime-current`, then `launchctl kickstart -k` the gateway.
8. **Verify (up to 60s)**: gateway process running from the new release path,
   `Gateway running with N platform(s)` with N ≥ 2 in `gateway.log`, and
   `:8642` listening. Then restart + verify the dashboard (`:9119` → HTTP 200).
9. On **any** verification failure: **automatic rollback** (repoint to
   `.previous`, restart, re-verify) and exit non-zero.

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
   repoint and (b) the `launchctl` restart, each funnelled through dedicated
   functions.
3. It **never** touches `~/.hermes/{config.yaml,*.db,cron/,scripts/,logs/,
   recovery/}`, `~/.config`, or `~/Library/LaunchAgents` (guarded by
   `assert_not_forbidden`; `logs/` is read-only for verification only).
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

## Rollback

```bash
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback
```

Repoints `runtime-current` to the release recorded in `releases/.previous`,
restarts both services, and re-verifies. No build. If the rollback restart does
not verify **both** gateway and dashboard health it exits non-zero and asks for
manual intervention rather than looping.
