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

Cross-repository Mini resources shared with `ignite-email-infra` (Purelymail poller
deploy staging under `~/.hermes/deploy/purelymail-poller/`) are governed by
`machine-setup/cross-repo-operating-contract.md`. Hermes-agent ships contract +
guards only; poller deploy implementation PRs land in ignite-email-infra.

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
4. **Gateway restart + verify** (up to 240s): process running from the new release
   path, `Gateway running with N platform(s)` (N ≥ 2), `:8642` listening, then the
   dashboard `:9119` returns HTTP 200.
5. On **any** verification failure: **automatic rollback** to `releases/.previous` and
   non-zero exit.

It never mutates persistent runtime state (`config.yaml`, `*.db`, `cron/`,
`logs/`, `~/.config`, `~/Library/LaunchAgents`). The sole operational-file
exception is source-controlled `clickup_workspace_refresh.py`: the cut stages,
SHA-256 verifies, atomically installs, and restores it on rollback.

### Run it (on the mini only)

```bash
# Standard cut of prod-live-patches:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches

# Polling-safe cut (equal=no-op, strict descendants only):
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --if-advanced

# Preview every mutating action, change nothing:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --dry-run

# Roll back to the previous release (no build):
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback
```

`--ref` defaults to `prod-live-patches`. `node`/`npm` live in `/opt/homebrew/bin`
(not on a non-interactive ssh PATH) — the script extends PATH itself.

## Executor handoff policy for `PLATFORM=manual` repos

**Tracking:** ClickUp `86e2ky2dk`.

The exemption above has a corollary the executor must honor. On a repo classified
`PLATFORM=manual`, **a CI-green PR is the complete executor deliverable.** Deploy
is operator/poller gated and is not in the executor's scope, so "I could not
deploy" is never a reason to hold a finished task.

Therefore, on a manual-platform repo:

- Finished, CI-green work goes to **In Review with a review packet** — the same
  terminal status the executor uses everywhere else. `ignite-validate` remains the
  exclusive completion gate.
- Work is **never** parked in In Progress behind an "ignite- BLOCKED HANDOFF"
  comment for an undeployable-platform reason. That handoff can never clear: the
  mini preflight is not something the executor can fix, and the task stalls the
  status handshake indefinitely. Two runs on 2026-08-03 (PRs #304 and #306, both
  CI-green) stalled exactly that way, which is what produced this policy.
- A genuine block (failing CI, missing credential, ambiguous scope) is still a
  block. This policy only removes *the deploy step* as a handoff precondition.

### The review packet

Every manual-platform handoff carries a packet stating:

1. the PR URL and its green CI evidence;
2. that `ignite-ship` classifies the repo `PLATFORM=manual` / `DEPLOY_ON_PUSH=false`
   and deploy is operator/poller gated;
3. **what to validate now** — the diff against acceptance criteria, plus tests and
   the linked green CI run;
4. **what to validate after the next release cut** — post-cut runtime behavior on
   the mini, since the change is inert until `runtime-current` is repointed.

A validator reading that packet must not treat "not deployed yet" as incomplete
work.

### Enforcement

The policy is enforced deterministically, not by prompt alone, by
`machine-setup/mini-scripts/manual_platform_handoff.py`. It runs on the closeout
cron's cadence (driven from `closeout_actor.py`'s `main()`), and is the exact
sibling of `closeout_actor`'s own sweep — the two are disjoint on PR state:

| Actor | Precondition | Result |
|---|---|---|
| `closeout_actor` | **MERGED** PR + validator PASS | task → `in review` |
| `manual_platform_handoff` | **OPEN** CI-green PR on a manual-platform repo | task → `in review` + review packet |

Manual-platform classification is read from `ignite-ship`'s own
`skills/ignite-ship/references/platform-hints.json` in the live `ignite-skills`
checkout, with a pinned floor (`hermes-agent`, `hermes-agent-upstream-contrib`) so
the policy still holds when that checkout is absent. A hint can add repos to the
manual set; it cannot declassify a repo on the floor.

Before any write, all of these must hold — a failure of any one is a logged skip,
never a silent one:

- the repo is manual-platform **and** on `~/.hermes/allowed-repos.txt` (a missing
  allowlist yields an empty target set — fail-closed);
- the PR is OPEN and links exactly one ClickUp task (an ambiguous PR is refused,
  not guessed);
- CI is green with at least one check, none failing or pending, and settled at
  least `--min-idle-minutes` (default 10) ago;
- `claim_store` reports **no live claim** on the task, so a running executor is
  never overtaken (an unreadable claim store counts as claimed);
- the task is in an advanceable in-flight status — anything already review- or
  complete-class is an idempotent no-op;
- the newest `ignite-validate:` marker is not FAIL/BLOCK.

Writes are ordered **packet first, then status flip**, so a task can never reach
the validator's queue in review without its packet; if the packet fails to post,
the status is left untouched. The flip goes through the guarded `clickup.mjs
status` path (which re-enforces G1/G2/G3 — Hermes never sets `complete`) and is
followed by a ClickUp read-after-write confirmation journaled as a
`review_handoff` event from source `manual-platform-handoff`.

Operate it directly with `manual_platform_handoff.py --dry-run` (reports, writes
nothing) or `--list-repos` (prints the resolved manual-platform target set).

## Why not just make `ignite-ship` deploy this repo?

`ignite-ship` is the deploy router for **web** projects (Vercel, Cloudflare
Workers/Pages, WordPress). Deploying `hermes-agent` means building a release on a
specific physical Mac mini and atomically flipping a launchd runtime symlink — there
is no push-to-main CI deploy to gate, and no cloud platform to target. Forcing it
into `ignite-ship` would misrepresent the deploy and risk exactly the kind of
unreviewed, state-destroying cut that the `2026-07-19` incident produced. The
exemption keeps `ignite-ship` honest and points operators at the one safe path.
