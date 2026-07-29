# fleet-config

Declarative fleet-config bundle for the Hermes mini, authored for the
2026-07-29 rebuild cutover. This is the sole writer of three destinations on
the mini:

| Source | Destination | What it does |
|---|---|---|
| `config-overlay.yaml` | `~/.hermes/config.yaml` | Governed **overlay** (not a replacement) — deep-merges `model`, `fallback_providers`, `delegation`, `kanban` in. Every other section (platforms, secrets wiring, security, approvals, credential_pool_strategies, ...) is left untouched. |
| `profiles/<name>/{config.yaml,SOUL.md}` | `~/.hermes/profiles/<name>/` | Five kanban-swarm profiles: `coder`, `content`, `design`, `research`, `ops`. Each gets its model config, its SOUL.md persona, and the full `_PROFILE_DIRS` bootstrap tree from `hermes_cli/profiles.py` (`memories`, `sessions`, `skills`, `skins`, `logs`, `plans`, `workspace`, `cron`, `home`). |
| `jobs.json` | `~/.hermes/cron/jobs.json` | Curated 16-job cron set (13 carried forward from the pre-freeze live config, 3 new consolidated hygiene/digest jobs). **Wholesale replace**, not a merge. |

`fleet_config_manifest.json` sha256-pins every source file above.
`install_fleet_config.py` verifies those hashes before writing anything,
snapshots each existing destination, writes atomically, and re-verifies the
deployed bytes.

## Why an overlay, not a full config.yaml replacement

`~/.hermes/config.yaml` carries hundreds of live, hand-tuned settings
(platforms, secret wiring, approvals, credential pool strategies, etc.) that
this bundle has no opinion on and must not clobber. The overlay only ever
touches the four keys it declares. See `install_fleet_config.py`'s
`merge_overlay()` docstring for the exact three-case merge rule — in
particular, `delegation: {}` in the overlay **replaces** the live delegation
block with `{}` (a deliberate reset), it does not "merge nothing." That
matters here: a past incident set `delegation.base_url` alongside a named
`delegation.provider` and leaked the parent cron's credential to a
third-party endpoint. Profile-based kanban routing replaces delegation-based
subagent routing going forward, so this bundle always ships delegation
cleared. **Never** re-introduce `delegation.base_url` + a named provider.

`fallback_providers` and `model` are scalar/list overlay keys — they
**replace** the live value wholesale, they don't append. Never use provider
`openai-api` (billed OpenAI) anywhere in this bundle; `openai-codex` (Codex
OAuth, subscription-covered) is the only sanctioned OpenAI surface.

## Jobs curation

Starting point: the pre-freeze snapshot at
`~/hermes-archive-20260729/jobs.json.pre-freeze` on the mini (36 jobs).

- **13 kept as-is** (config unchanged): `clickup-poll-gate`,
  `review-poll-gate`, `clickup-review-sla`, `hermes-pr-validate` (kept
  disabled, matching its prior paused state), `spend-meter`,
  `ci-health-watch`, `clickup-workspace-refresh`, `reap-stranded-claims`,
  `ignite-board-sync`, `clickup-closeout-actor`, `Purelymail notify-me
  poller`, plus `clickup-executor` and `content-lane-executor` (config kept,
  **prompt modified** — see below).
- **3 new consolidated jobs**: `clickup-lifecycle` (every 30m; folds
  clickup-closeout-audit + clickup-stalled-reconciler + clickup-reconciler +
  staleness-sweep), `fleet-health-digest` (every 6h; folds hermes-self-report
  + delivery-probe + skill-size-monitor + model-deprecation-check +
  supabase-rls-guard + hermes-usage-alert), `repo-maintenance` (daily 04:00;
  folds repo-worktree-gc + cleanup-hermes-baks + orphan-unpushed-monitor).
  Each is an LLM-orchestrated job (not `no_agent`) whose prompt runs the
  original per-check scripts via Bash, in order, and folds any non-clean
  result into its report — the underlying scripts are unchanged, only the
  cron entry that invoked them standalone is retired.
- **8 dropped outright**: `email-triage`, `clickup-email-triage-gate`, `w`,
  `alpha`, `Spam-gate label accrual`, `legacy job`, `run {lane}`,
  `clickup-executor-2`.
- `pr-staleness-alert` and `research-stage-monitor` are dropped as
  **standalone** jobs. `research-stage-monitor`'s check is folded into
  `content-lane-executor`'s prompt (it's already agent-driven, so adding a
  Bash step is free). `pr-staleness-alert`'s daily check is **not** folded
  into `ci-health-watch` at the script level — see Deviations below.

### Modified: clickup-executor / content-lane-executor

Both jobs now claim a task, then **hand it to a kanban swarm** instead of
working it in-session:

```
hermes kanban swarm "<task goal>" --worker coder:"implement" --verifier ops \
  --synthesizer coder --idempotency-key <clickup-task-id> --json
```

(`content-lane-executor` uses `--worker content:"draft"`.) The job polls for
the synthesizer to reach `done`, then posts the result back to ClickUp as a
comment and moves the task to **In Review** — never Complete;
`ignite-validate` owns Complete.

## Deploy

```bash
# 1. Verify the plan/diff without touching anything (safe against any home,
#    including the real mini home).
python3 machine-setup/fleet-config/install_fleet_config.py --dry-run

# 2. Run it for real — after the release cut, before the mini starts taking
#    live cron traffic on the new config.
python3 machine-setup/fleet-config/install_fleet_config.py

# Non-default home (e.g. staging a fixture, or testing):
python3 machine-setup/fleet-config/install_fleet_config.py --home /path/to/home --dry-run
```

The installer fails closed on: any source file's sha256 not matching the
manifest, an existing `~/.hermes/config.yaml` that isn't valid YAML, a
rendered merge result that doesn't round-trip through a YAML parse, a
bundled `jobs.json` that isn't valid JSON or has no top-level `jobs` list,
or a deployed file whose re-read hash doesn't match the manifest. Any
mid-install failure rolls back every step already written in that run from
its snapshot.

## Rollback

Every run writes timestamped snapshots to
`~/.hermes/logs/fleet-config-installs/<UTC-ts>/` (a copy of the manifest
plus every destination's pre-install snapshot) and drops a
`<dest>.bak-fleet-config[-<detail>]-install-<UTC-ts>` sibling next to each
destination file it touched. To roll back by hand:

```bash
cp ~/.hermes/config.yaml.bak-fleet-config-install-<ts>              ~/.hermes/config.yaml
cp ~/.hermes/cron/jobs.json.bak-fleet-config-jobs-install-<ts>      ~/.hermes/cron/jobs.json
cp ~/.hermes/profiles/<name>/config.yaml.bak-fleet-config-profile-<name>-install-<ts> \
   ~/.hermes/profiles/<name>/config.yaml
```

The full receipt (`install-receipt.json` in the same snapshot dir) records
every destination touched, its snapshot path, and the exact diff applied —
use it to find the right timestamp/paths rather than guessing.

## Deviations from the original spec

- **`pr-staleness-alert` fold into `ci-health-watch`**: not implemented at
  the script level. `ci-health-watch` runs every 5 minutes as a pure
  `no_agent` script (`ci_health_watch.py`, a reviewed PR-pipeline
  trust-boundary component); converting it to an LLM-orchestrated job to
  bolt on a once-daily extra check would 12x its cron cost for a monitor
  that currently costs nothing, and hand-editing `ci_health_watch.py`'s
  logic blind (its source lives in this repo but is security-reviewed
  trust-boundary code) was judged out of scope for a config-only bundle.
  `pr-staleness-alert` is dropped as a standalone cron entry per spec; the
  actual daily-check fold is left as a follow-up requiring a small wrapper
  script in the `ci_health_watch.py` style (see
  `research-stage-monitor-cron.py` for the established "thin wrapper bakes
  in a flag/extra call" pattern this repo already uses for the same
  constraint — mini cron jobs can't pass argv).
- **Profile bootstrap dirs**: the task text listed 8 dirs (memories,
  sessions, skills, logs, plans, workspace, cron, home); the installer
  creates the real 9 from `hermes_cli/profiles.py::_PROFILE_DIRS`, which
  also includes `skins`. Using the actual constant (rather than a
  hand-copied list that could drift) keeps this bundle in sync with
  `hermes profile create` automatically.
- **jobs.json runtime fields**: `last_run_at`, `next_run_at`, `last_status`,
  `last_error`, `last_delivery_error`, and `repeat.completed` are reset to
  null/0 for every job (not carried over from the pre-freeze snapshot) since
  this is a rebuild cutover, not an in-place patch. Original job `id`s are
  preserved for the 13 kept jobs (some tooling — e.g.
  `validator_repo_guard` callers — references `hermes-pr-validate`'s id
  `5a76e290811d` by value); the 3 new jobs get freshly generated ids.
