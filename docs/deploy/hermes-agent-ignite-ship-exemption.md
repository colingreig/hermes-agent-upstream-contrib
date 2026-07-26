# hermes-agent is an intentional `ignite-ship` exemption

**Status:** sanctioned exemption. `ignite-ship` does **not** deploy `hermes-agent`.
**Tracking:** ClickUp `86e2ddah5`.

## TL;DR

`ignite-ship` reports `PLATFORM=manual` / `DEPLOY_ON_PUSH=false` for `hermes-agent`.
That is **correct and expected** — `hermes-agent` is not a Vercel / Cloudflare /
WordPress web project. It is a launchd service running on a physical Mac mini, and
it is **not** deployed by pushing to `main`.

- **Merging a PR to `main` does NOT deploy anything to production.** The mini does
  not run from GitHub `main`.
- The sanctioned deploy path is the committed **mini release-cut script**
  (see below), run **on the mini** via the service-account path.
- Do **not** try to "fix" the `ignite-ship` classification by wiring `hermes-agent`
  into Vercel/CF. The exemption is the intended state.

A matching platform hint in `ignite-skills`
(`skills/ignite-ship/references/platform-hints.json`, key `hermes-agent`) classifies
this repo as `PLATFORM=manual` (`DEPLOY_ON_PUSH=false`) so `ignite-ship` surfaces this
exemption instead of the bare `unknown` classification reported before that hint
was added.

## How production actually runs

The mini gateway runs from the `~/.hermes/runtime-current` symlink, which points
into a **frozen release snapshot** under `~/.hermes/releases/v<version>-<12charsha>/`.
Each snapshot is a self-contained git clone + its own `venv/` + built
`hermes_cli/web_dist/`. The active release is whatever `runtime-current` currently
targets; `~/.hermes/releases/.previous` records the prior target for rollback.

- Gateway: launchd `gui/501/ai.hermes.gateway` (API on `:8642`).
- Dashboard: launchd `gui/501/com.colingreig.hermes-dashboard` (`:9119`).

Because the runtime is a frozen snapshot off a local **`prod-live-patches`** branch,
merges to `main` (or to the contrib fork) do **not** reach production until a new
release is cut and the `runtime-current` symlink is atomically repointed.

## The sanctioned deploy path — mini release-cut script

**Tracked path:** `scripts/mini-release-cut.sh` (with `scripts/MINI-RELEASE.md`),
tracked on the **`prod-live-patches`** branch — the branch the mini builds from.
It is **not** on `main`. On the mini it is present at
`~/.hermes/runtime-current/scripts/mini-release-cut.sh`.

This is the committed, reviewable automation added after the **2026-07-19** incident,
in which an improvised, uncommitted cutover to the `releases/` + `runtime-current`
layout destroyed runtime state (SQLite DBs truncated under live WAL connections;
`config.yaml`, the auth token, and LaunchAgents deleted). The script exists so a cut
is safe, reviewable, and reproducible.

What a cut does (see `scripts/MINI-RELEASE.md` for the authoritative detail):

1. **Stage-build** an entirely new `releases/v<version>-<12charsha>/` (git clone +
   `uv`-managed venv + `npm run build --workspace web` → `hermes_cli/web_dist/`).
   Refuses to run if the target dir already exists (no in-place mutation).
2. **Verify importable** before any switch: `python -c "import hermes_cli.main"` and
   `hermes_cli/web_dist/index.html` present.
3. **Atomic symlink flip** of `runtime-current` (`ln -sfn` temp + `mv -fh`), recording
   the prior target to `releases/.previous`.
4. **Gateway restart + verify** (up to 60s): process running from the new release
   path, `Gateway running with N platform(s)` (N ≥ 2), `:8642` listening, then the
   dashboard `:9119` returns HTTP 200.
5. On **any** verification failure: **automatic rollback** to `releases/.previous` and
   non-zero exit.

It never mutates live runtime state (`config.yaml`, `*.db`, `cron/`, `scripts/`,
`logs/`, `~/.config`, `~/Library/LaunchAgents`) — that invariant is enforced in code.

### Run it (on the mini only)

```bash
# Standard cut of prod-live-patches:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches

# Preview every mutating action, change nothing:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --dry-run

# Roll back to the previous release (no build):
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback
```

`--ref` defaults to `prod-live-patches`. `node`/`npm` live in `/opt/homebrew/bin`
(not on a non-interactive ssh PATH) — the script extends PATH itself.

## Why not just make `ignite-ship` deploy this repo?

`ignite-ship` is the deploy router for **web** projects (Vercel, Cloudflare
Workers/Pages, WordPress). Deploying `hermes-agent` means building a release on a
specific physical Mac mini and atomically flipping a launchd runtime symlink — there
is no push-to-main CI deploy to gate, and no cloud platform to target. Forcing it
into `ignite-ship` would misrepresent the deploy and risk exactly the kind of
unreviewed, state-destroying cut that the `2026-07-19` incident produced. The
exemption keeps `ignite-ship` honest and points operators at the one safe path.
