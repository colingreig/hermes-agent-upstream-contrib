# fleet-config

Declarative fleet-config bundle for the Hermes mini, authored for the
2026-07-29 rebuild cutover. This is the sole writer of three destinations on
the mini:

| Source | Destination | What it does |
|---|---|---|
| `config-overlay.yaml` | `~/.hermes/config.yaml` | Governed **overlay** (not a replacement) — deep-merges `model`, `fallback_providers`, `delegation`, `kanban` in. Every other section (platforms, secrets wiring, security, approvals, credential_pool_strategies, ...) is left untouched. |
| `profiles/<name>/{config.yaml,SOUL.md}` | `~/.hermes/profiles/<name>/` | Five kanban-swarm profiles: `coder`, `content`, `design`, `research`, `ops`. Each gets its model config, its SOUL.md persona, and the full `_PROFILE_DIRS` bootstrap tree from `hermes_cli/profiles.py` (`memories`, `sessions`, `skills`, `skins`, `logs`, `plans`, `workspace`, `cron`, `home`). The `home/` entry is a subprocess workspace, not the profile's Hermes root. |
| `jobs.json` | `~/.hermes/cron/jobs.json` | Curated 16-job cron set (13 carried forward from the pre-freeze live config, 3 new consolidated hygiene/digest jobs). **Wholesale replace**, not a merge. |

`fleet_config_manifest.json` sha256-pins every source file above.
`install_fleet_config.py` verifies those hashes before writing anything,
snapshots each existing destination, writes atomically, and re-verifies the
deployed bytes.

## Production release path

The rebuilt fleet has one production release path: an operator manually runs
the governed cutter with the exact certified full SHA and its immutable
promotion receipt ID:

```bash
~/.hermes/runtime-current/scripts/mini-release-cut.sh \
  --ref <certified-full-sha> \
  --certified-sha <same-certified-full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256>
```

The automatic LaunchAgent `com.colingreig.hermes.release-poll` is retired for
this fleet and is not installed, loaded, or enabled by the fleet bundle. Do not
bootstrap it as part of fleet setup or normal release operations. The generic
poller scripts and tests remain in the repository as an opt-in contingency for
a deployment that explicitly adopts that operating model; they are not a
second Hermes fleet production path.

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

## Profile credential placement

A named profile's Hermes root is `~/.hermes/profiles/<name>/`. Its canonical
provider credential store is therefore:

```text
~/.hermes/profiles/<name>/auth.json
```

The kanban dispatcher sets `HERMES_HOME` to that profile root before resolving
model credentials. Do not put Hermes provider credentials in
`~/.hermes/profiles/<name>/home/auth.json`: `home/` is a required
`_PROFILE_DIRS` bootstrap directory used when a subprocess needs a
profile-scoped `HOME`; it is not the Hermes auth root and must not be removed
from the installer to work around credential placement.

This distinction is fail-open at the profile boundary: when the canonical
profile store is absent, the auth layer may read the global
`~/.hermes/auth.json` as a fallback. A model call can therefore succeed while
silently using the global account. Credential verification must inspect the
safe `auth_store` and `source` fields from the runtime auth status and run a
no-fallback completion from the actual profile root. For a dedicated coder
account, the expected values are
`auth_store=~/.hermes/profiles/coder/auth.json` and
`source=pool:codex-pro-2`.

Install a dedicated profile store as a mode-`600` regular file, using an
atomic replace and a timestamped backup when a destination already exists.
Receipts may record paths, credential IDs/labels, and before/after file
metadata, but never tokens, token hashes, or fingerprints. Verify that the
global auth file's identity, size, and modification time are unchanged across
both installation and the profile-serving proof.

## Fallback chain

```
primary   openai-codex / gpt-5.5            (Codex OAuth, subscription-covered)
tier 2    zai / glm-4.7                     (GLM_API_KEY/ZAI_API_KEY, already live)
tier 3    nous / moonshotai/kimi-k2.6       (Nous Research Portal, key PENDING)
```

- **google/gemini-2.5-flash is removed** from the chain (2026-07-29). Its
  history on this fleet is repeated false credential alarms (`.env`
  `${GEMINI_API_KEY}` interpolation poison + sticky exhausted-pool cache),
  and it added a third billing surface for no capability the other tiers
  lack.
- **Tier 3 = Nous Research Portal** (https://portal.nousresearch.com),
  OpenAI-compatible inference at `https://inference-api.nousresearch.com/v1`
  (public `/v1/models` verified 2026-07-29; 323 models). `nous` is a
  first-class Hermes provider — the base URL is baked into
  `PROVIDER_REGISTRY`, so the fallback entry needs no `base_url` (and per
  the delegation credential-leak lesson we don't set one next to a named
  provider). Model choice, from live catalog pricing:
  - **`moonshotai/kimi-k2.6`** (chosen, in-chain): $0.5168/M in, $2.176/M
    out, 262k ctx, tool-calling supported — the cheapest current-gen Kimi
    that can actually run the agent loop (k2.5 is $0.456/$2.28 but older
    gen; kimi-k3 is $2.40/$12.00).
  - **`nousresearch/hermes-4-70b`** (recorded secondary, NOT in-chain):
    $0.05/M in, $0.20/M out — cheapest on the Portal, but it does not
    support tool-calling and Nous' own docs say Hermes-4 is not
    recommended for agent work, so it must not sit in an agent fallback
    chain.
- **PENDING: `NOUS_PORTAL_API_KEY` is not yet provisioned.** The fallback
  entry sources its key via `key_env: NOUS_PORTAL_API_KEY`
  (`hermes_cli/fallback_config.py::resolve_entry_api_key` → passed as
  `explicit_api_key`; no OAuth needed). NEVER hardcode the key, and never
  write it as a `${VAR}` reference inside a `.env`-style value —
  `load_env` does not interpolate and a literal `${...}` poisons the
  credential pool (the gemini-400 incident). Until the key lands in the
  gateway secret env (1Password → launchd wrap), tier 3 falls through to
  the (also unprovisioned) nous OAuth store and the chain effectively
  ends after zai — same coverage as before this change, minus the gemini
  false-alarm surface.
- Do **not** add a `providers:`/`custom_providers:` entry named `nous`:
  `runtime_provider.py::_get_named_custom_provider` defers canonical
  built-in names to the built-in provider, so such an entry is dead
  config. (The installer's deep-merge would handle a nested `providers:`
  map correctly — non-empty dicts merge key-by-key, preserving live
  sibling providers — it's the runtime that would ignore a custom `nous`.)
- The **content profile is exempt by design**: `profiles/content/config.yaml`
  stays `anthropic/claude-sonnet-5` with `fallback_providers: []`
  (fail-closed; model substitution on content is a defect).

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
  Bash step is free). `ci-health-watch-cron.py` now provides the
  `pr-staleness-alert` daily fold without changing either underlying
  monitor; the bundled `jobs.json` remains unchanged because the live cron
  rewire is a separate ops step — see Deviations below.

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

The internal kanban lifecycle is deliberately separate from that ClickUp
lifecycle. Each successful worker, verifier, and synthesizer must complete its
own card (`kanban_complete`; CLI equivalent:
`hermes kanban complete <card-id> --result "..."`) so dependent cards can run.
That internal `done` never means ClickUp Complete. `kanban_block` is only for a
genuine blocker, not for enforcing the ClickUp In Review rule; the outer
executor is the sole bridge that posts the final swarm result to ClickUp.

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

- **`pr-staleness-alert` fold into `ci-health-watch`**: implemented by the
  argv-free `machine-setup/mini-scripts/ci-health-watch-cron.py` follow-up.
  The wrapper always runs the reviewed `ci_health_watch.py` unchanged and
  propagates its output and exit code. At most once per rolling 24 hours,
  gated by the distinct atomic state file
  `~/.hermes/state/ci-health-pr-staleness-last-run.json`, it also invokes
  the existing `pr_staleness_alert.py` scan and routes emitted alerts through
  `hermes send --to slack:hermes`. This preserves the five-minute pure
  `no_agent` monitor and avoids both LLM cron cost and trust-boundary edits.
  The fleet bundle's `jobs.json` intentionally has no change; pointing the
  live `ci-health-watch` job at this wrapper remains a separate ops step.
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
