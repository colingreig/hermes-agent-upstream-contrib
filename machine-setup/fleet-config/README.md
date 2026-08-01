# fleet-config

Declarative fleet-config bundle for the Hermes mini, authored for the
2026-07-29 rebuild cutover and governed by the 2026-08-01 skill-surface
hardening rollout. This is the sole writer of four governed surfaces on the
mini:

| Source | Destination | What it does |
|---|---|---|
| `config-overlay.yaml` | `~/.hermes/config.yaml` | Governed **overlay** (not a replacement) — deep-merges `model`, `fallback_providers`, `delegation`, `kanban` in. Every other section (platforms, secrets wiring, security, approvals, credential_pool_strategies, ...) is left untouched. |
| `profiles/<name>/{config.yaml,SOUL.md}` | `~/.hermes/profiles/<name>/` | Five direct-execution profiles: `coder`, `content`, `design`, `research`, `ops`. Each gets its model config, its SOUL.md persona, and the full `_PROFILE_DIRS` bootstrap tree from `hermes_cli/profiles.py` (`memories`, `sessions`, `skills`, `skins`, `logs`, `plans`, `workspace`, `cron`, `home`). The `home/` entry is a subprocess workspace, not the profile's Hermes root. |
| `jobs.json` | `~/.hermes/cron/jobs.json` | Curated 15-job cron set (12 carried forward from the pre-freeze live config, 3 new consolidated hygiene/digest jobs). **Wholesale replace**, not a merge. |
| `skills-policy.json` | Default plus five profile-local `skills/` trees | SHA-pinned allowlist/cull policy. Preserves allowed bundled skills, archives and suppresses removals, consolidates two historical ClickUp poller references, removes the obsolete local `sentry-monitor` wrapper without touching the operational Ignite Sentinel checkout, and removes the local hub shadow of `vehicle-image-qc` only with exact hub provenance. |
| `install_fleet_config.py` | Operator entry point (source-only manifest pin) | Verifies the whole bundle, performs the governed mutations, and records rollback receipts. The installer itself is SHA-256 pinned by the same manifest before any mutation. |

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
third-party endpoint. The fleet's direct ClickUp executors do not need that
delegation path, so this bundle always ships delegation cleared. **Never**
re-introduce `delegation.base_url` + a named provider.

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

Profile-scoped Hermes runs resolve credentials from that profile root. Do not
put Hermes provider credentials in
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
tier 3    nous / deepseek/deepseek-v4-pro   (Nous Research Portal, effort high)
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
  provider). Model choice, from live catalog pricing and Artificial Analysis
  benchmarks (2026-07-31):
  - **`deepseek/deepseek-v4-pro`**, effort **`high`** (chosen, in-chain):
    $0.348/M in, $0.696/M out, 1M ctx, tool-calling + structured output,
    named reasoning efforts `high`/`xhigh` — ~54% cheaper input and ~78%
    cheaper output vs the prior Kimi K2.6 rung, with comparable intelligence
    (43.1 / 58.7 coding / 34.4 agentic on AA). DeepSeek explicitly supports
    `high`, so Hermes can set effort without the unsupported-`medium` failure
    mode that blocks MiniMax M3 today.
  - **`moonshotai/kimi-k2.6`** (prior rung, superseded): $0.5168/M in,
    $2.176/M out — retained here only as the documented prior choice.
  - **`nousresearch/hermes-4-70b`** (recorded secondary, NOT in-chain):
    $0.05/M in, $0.20/M out — cheapest on the Portal, but it does not
    support tool-calling and Nous' own docs say Hermes-4 is not
    recommended for agent work, so it must not sit in an agent fallback
    chain.
- **`NOUS_PORTAL_API_KEY` is provisioned in 1Password** at
  `op://hermes-agent/nous_portal_api_key/password` (secure note in the
  `hermes-agent` vault). The fallback entry sources it via
  `key_env: NOUS_PORTAL_API_KEY` (`hermes_cli/fallback_config.py::
  resolve_entry_api_key` → passed as `explicit_api_key`; no OAuth needed).
  Add this line to the live `~/.hermes/scripts/op-secrets.env` manifest on
  the mini (lazy resolution — not boot-exported):
  `NOUS_PORTAL_API_KEY=op://hermes-agent/nous_portal_api_key/password`.
  NEVER hardcode the key, and never write it as a `${VAR}` reference inside
  a `.env`-style value — `load_env` does not interpolate and a literal
  `${...}` poisons the credential pool (the gemini-400 incident).
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

- **12 carried forward by stable job ID and schedule**: `clickup-poll-gate`,
  `review-poll-gate`, `clickup-review-sla`, `hermes-pr-validate` (kept
  disabled at cutover time, matching its prior paused state — see
  "Re-enablement" below), `spend-meter`, `ci-health-watch`,
  `clickup-workspace-refresh`, `reap-stranded-claims`, `ignite-board-sync`,
  `clickup-closeout-actor`, plus `clickup-executor` and
  `content-lane-executor` (config kept, **prompt
  modified** — see below).

### Re-enablement: hermes-pr-validate (2026-07-31, task `86e2k3qe1`)

`hermes-pr-validate` was carried forward **disabled** at the 2026-07-29
rebuild cutover, matching its pre-freeze paused state — it was not
re-evaluated at that time, just preserved as-is. Its provider/model/skill
config was already correct going into this fleet bundle: `skill:
ignite-validate` (not the never-existent `hermes-pr-validate` skill),
`max_turns: 200`, and `workdir: /Users/colingreig/dev/hermes-agent` (the
RC1/RC2 2026-07-26 fixes documented in the job's own `prompt` field — see
`jobs.json`). `provider: openai-codex` / `model: gpt-5.4-mini` are
operator-set and were not touched.

The 2026-07-31 re-enablement flipped it back on: `enabled: true`, `state:
scheduled`, `paused_at: null`. That change left provider, model, skill,
`max_turns`, and `workdir` untouched. The matching
`fleet_outcome_contracts.json` entry (id `5a76e290811d`) and
[MONITOR_COVERAGE.md](MONITOR_COVERAGE.md) row were updated in the same
change: the contract previously *asserted the job stays disabled*
(`enabled: false`, `max_age_seconds: 0`) and would have alarmed
`enabled_state_drift` the moment this job ran live. It now expects
`enabled: true` with a 7200s freshness budget (matching the hourly `0 * *
* *` schedule plus one missed-run allowance) and `cron_output`
success/failure patterns modeled on the other LLM-orchestrated executor
jobs (`ignite-validate`/`PASS`/`FAIL` markers as success, hard execution
failures or the `validator_repo_guard.py` safety-guard `ABORT` output as
failure).

The validator job also requires `IGNITE_SKILLS_ROOT` and invokes its ClickUp
and Ignite State modules only below that canonical root. It never probes or
falls back to `~/.hermes/skills`, `~/.claude/skills`, or `~/.codex/skills`;
a missing canonical module is a fail-closed job error, not a reason to create
a home-local copy or symlink.

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
  **standalone** jobs. The direct `content-lane-executor` prompt is reserved
  for `/ignite-execute --lane content`; it does not carry a hidden monitor
  side job. `ci-health-watch-cron.py` provides the `pr-staleness-alert` daily
  fold without changing either underlying monitor. The bundled `jobs.json`
  points `ci-health-watch` at that argv-free wrapper so the fold and its
  independent fleet-probe watchdog are live.

### Modified: clickup-executor / content-lane-executor

Both jobs execute ClickUp work directly in their scheduled Hermes session.
Their prompts are deliberately small and exact:

```
clickup-executor:      Work the ClickUp queue now — follow the clickup-queue-poller skill.
content-lane-executor: /ignite-execute --lane content
```

The root-scheduled content job does not inherit the root profile's inference
route. It is explicitly pinned through the cron job contract to
`provider: anthropic`, `model: claude-sonnet-5`, and `no_fallback: true`, and
loads `ignite-execute` under `skill_scope: content-executor`. This preserves
the content profile's Sonnet-only, fail-closed behavior while keeping the
approved direct `/ignite-execute --lane content` path in the governed root
cron store.

The invoked skills own claiming, implementation, validation, handoff comments,
and the move to **In Review**. They never move tasks to Complete;
`ignite-validate` owns Complete. Upstream Hermes kanban commands remain
available as a product feature, but these fleet ClickUp jobs and their profile
SOULs do not depend on a worker/verifier/synthesizer pipeline or scratch-file
handoff.

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
an unmanifested or hash-drifted skill policy, historical reference provenance
drift, conflicting archive bytes, hub-lock identity drift,
an exact-pattern root legacy config backup that is a symlink, non-regular
entry, owned by a different user, or resolves outside the Hermes root,
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

The installer also discovers only root-level legacy siblings matching
`~/.hermes/config.yaml.bak-fleet-config-install-<YYYYMMDDTHHMMSSZ>` and
normalizes those regular files to mode `0600`. Dry-run reports every planned
mode transition. The receipt journals each file's exact prior mode and inode,
and an interrupted install restores that mode during rollback. Re-running is
idempotent. Similar loose names, nested files, and all profile config backups
are intentionally untouched.

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
  The fleet bundle now points the live `ci-health-watch` job at this wrapper.
  The wrapper also cross-watches the independent LaunchAgent outcome probe's
  heartbeat; the LaunchAgent, in turn, verifies the five-minute CI job's
  fresh parsed CI state (stable lifecycle, VM available, no resource drift).
  This breaks the prior circular self-report
  arrangement while preserving `ci_health_watch.py` byte-for-byte.
- **Coverage-verified monitor plane**: task `86e2jbbhx` adds the complete
  contract and alarm matrix in [MONITOR_COVERAGE.md](MONITOR_COVERAGE.md).
  `fleet_outcome_probe.py` runs as an independent five-minute LaunchAgent,
  fails closed on uncovered enabled jobs or Hermes LaunchAgents, and routes
  delivery-aware Slack alarms without an LLM.
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
  preserved for the 12 kept jobs (some tooling — e.g.
  `validator_repo_guard` callers — references `hermes-pr-validate`'s id
  `5a76e290811d` by value); the 3 new jobs get freshly generated ids.
