# Mini-local scripts (canonical copies)

These scripts run on the Mac mini at `~/.hermes/scripts/` but live **outside**
the immutable release directory. Most remain manual-copy assets; governed
exceptions below are installed transactionally by `scripts/mini-release-cut.sh`
with source/deployed identity checks and rollback snapshots.

**Sibling bundle — `../fleet-config/`.** The manifest-verified,
sha256-pinned, fail-closed installer pattern established here
(`self_report_manifest.json` + `install_self_report.py`,
`spend_manifest.json` + `install_spend.py`) is also used for the
2026-07-29 rebuild's `fleet-config` bundle at `machine-setup/fleet-config/`,
which governs three different destinations: a `config.yaml` **overlay**
(deep-merge, not replace), the five kanban-swarm profiles under
`~/.hermes/profiles/`, and a curated `~/.hermes/cron/jobs.json`
(wholesale replace). See `machine-setup/fleet-config/README.md` for that
bundle's own deploy/rollback instructions — it is a separate installer
(`install_fleet_config.py`) with its own manifest, not part of this
directory's manual-copy convention.

The 2026-07-19 mini home-directory data-loss incident (see ClickUp 86e2ddcpb)
proved that gap real: the `op_sdk_resolve.py` resilience patch (300s cache +
retry/backoff + serve-stale, added 2026-07-13 after a ~13h 1Password
daily-quota lockout) was silently lost in the wipe/recovery and nobody
noticed until this task re-verified it (86e2a99q9, 2026-07-21).

**Convention going forward:** any `~/.hermes/scripts/*` file (`.py` or `.sh`)
that fixes a production incident gets a canonical copy committed here, in git,
so it survives even a full home-directory loss — not just a
`~/.hermes/local-patches` copy (that directory itself was lost in the same
incident).

To restore a script after any kind of mini data loss:

```bash
scp machine-setup/mini-scripts/<file> mac-mini-h.tail51ec1b.ts.net:~/.hermes/scripts/<file>
mini-run -- 'python3 -m py_compile ~/.hermes/scripts/<file>'  # sanity check
```

**Destination path note:** every file below deploys to `~/.hermes/scripts/<file>`
EXCEPT `clickup-queue-poller-claim_next.py`, whose mini destination is
`~/.hermes/skills/clickup-queue-poller/scripts/claim_next.py` (a different
directory tree, the skill's own scripts dir — the filename here is prefixed
`clickup-queue-poller-` only to keep it collision-free in this flat repo
directory; drop the prefix when copying to the mini). See "Claim retry cap"
below for the exact copy commands.

A second exception: `repo-aliases.json` deploys to
`~/.hermes/config/repo-aliases.json` (not the scripts dir). See "Validator
repository identity" below.

## Validator repository identity (`pr_pipeline/validator_repo_guard.py`, `repo-aliases.json`)

Guards the `hermes-pr-validate` cron (id `5a76e290811d`) against spurious
`class=wrong-repo` FAIL verdicts. Two root causes, both fixed 2026-07-26:

- **RC1 — wrong workdir.** The job's `workdir` was hardcoded to
  `/Users/colingreig/dev/thermal` while the job is chartered for the
  hermes-agent board, so ignite-validate resolved the wrong board/repo.
  Repointed in `jobs.json`; `--expect-repo` mode is the standing tripwire.
- **RC2 — repo identity compared as a NAME STRING.** On 2026-07-23
  `colingreig/hermes-agent` was renamed to
  `colingreig/hermes-agent-upstream-contrib`. GitHub redirects the old name, so
  both spellings address ONE repo — but the mini holds both simultaneously
  (`~/dev/hermes-agent` origin uses the OLD name; the bare mirror and every
  `wt-new` worktree use the NEW one; Execution Briefs say `Target repo:
  hermes-agent`; handoff PR/CI URLs say `...-upstream-contrib`). Every literal
  comparison of those two spellings produced a false `wrong-repo` FAIL against
  correct, merged work (86e2gh04e, 86e2gdmfk, 86e25xww8, 86e2f7ukm — all
  manually voided).

The fix resolves every repo reference to its **canonical GitHub identity**
(`gh api repos/<owner>/<name> --jq .node_id`, which follows renames) and
compares identities, never names. That is deliberately generic: the *next*
rename needs no code change and no alias entry.

```bash
# Mode A — is this checkout the repo this cron is chartered for?
python3 ~/.hermes/scripts/validator_repo_guard.py --expect-repo hermes-agent --workdir "$(pwd)"
# Mode B — is the evidence PR/CI URL the SAME repo as the task's target?
python3 ~/.hermes/scripts/validator_repo_guard.py --compare hermes-agent <pr-or-ci-url>
# Mode C — debug one reference
python3 ~/.hermes/scripts/validator_repo_guard.py --identity hermes-agent
```

Exit codes — **0** same/OK, **3** ABORT (skip the task, write NO verdict and
change NO status), **4** confirmed different repositories (a `wrong-repo` FAIL
is authorized). Exit 3 is the fail-closed default whenever identity cannot be
established: a spurious FAIL kicks real reviewed work back to `to do` and burns
multi-day rework loops, while a skip costs one hourly cycle.

Two supporting notes:

- **Definite-404 inference.** The mini's App token (`hermes-dev-assistant[bot]`)
  is installed per repository, so some real repos 404 for it (e.g.
  `colingreig/hermes-ops-scripts`, the genuine wrong-repo in 86e2eu4fp). Since
  both the rename redirect and installation access key on the repository *id*,
  a definite HTTP 404 against a credential that just resolved the target LIVE
  proves the evidence repo is not a rename alias of the target → `DIFFERENT`.
  A non-404 `gh` failure proves nothing → `UNRESOLVED`/skip.
- **`repo-aliases.json`** (`~/.hermes/config/repo-aliases.json`, override
  `$HERMES_REPO_ALIASES`) is an offline **last resort only** — it short-circuits
  the network for a known rename when the mini is offline with a cold identity
  cache. Prefer letting `gh` follow the redirect. The identity cache lives at
  `~/.hermes/state/repo_identity_cache.json` (7-day TTL, stale entries served
  when `gh` is unreachable).

```bash
# The guard's canonical source is machine-setup/mini-scripts/pr_pipeline/validator_repo_guard.py.
# It is manifest-managed — do NOT scp it by hand; deploy the whole boundary:
ACTIVE_RELEASE="$(ssh mini 'readlink "$HOME/.hermes/runtime-current"')"
python3 machine-setup/mini-scripts/reconcile_pr_pipeline.py install \
  --host mini --source-commit <sha> \
  --runtime-python "$ACTIVE_RELEASE/venv/bin/python"
# (installs both ~/.hermes/scripts/validator_repo_guard.py and
#  ~/.hermes/scripts/pr_pipeline/validator_repo_guard.py — the CLI paths above
#  are unchanged.)
scp machine-setup/mini-scripts/repo-aliases.json      mini:~/.hermes/config/repo-aliases.json
python3 machine-setup/mini-scripts/tests/test_validator_repo_guard.py            # hermetic
HERMES_REPO_GUARD_LIVE=1 python3 machine-setup/mini-scripts/tests/test_validator_repo_guard.py  # + real gh
```

The PR review/merge closure is the exception to the manual-copy convention:
its canonical sources, deterministic manifest, and deployment reconciler live
under `pr_pipeline/`. Do not compare or copy those files one at a time.

## Adversarial review pass (`pr_pipeline/adversarial_review.py`, ClickUp 86e2k3qe2)

Explicit, testable module tying together three failure classes an adversarial
reviewer must actively hunt for in an in-review/needs-validation task, before
a verdict can be finalized as PASS. Each reuses machinery this fleet already
built and fixes a real gap where the reuse was missing:

- **wrong-repo** — `check_wrong_repo()` delegates to
  `validator_repo_guard.compare_refs()` (canonical GitHub node_id, rename-
  following, alias-aware). It replaces a naive lowercased-name-string compare
  that used to live inline in `validate_tripwires.run()` — exactly the RC2
  bug class documented in "Validator repository identity" above (a rename
  reads as two different repos and manufactures a spurious `wrong-repo`
  BLOCK against real, merged work). `validate_tripwires.run()` now calls
  `adversarial_review.check_wrong_repo()` instead of comparing strings
  itself, so its `--expected-repo` CLI flag keeps working but can no longer
  reintroduce that bug.
- **stale-evidence** — `check_stale_evidence()` flags a diff/verdict whose
  recorded head SHA no longer matches the PR's live head: the reviewed
  snapshot was superseded by a newer commit. `validate_pr.py` already
  enforces this for its own read (fail-closed BLOCK right after fetching the
  diff); this function exposes the same guarantee as a standalone, reusable,
  independently testable check for any other caller.
- **missing-ci** — `check_missing_ci()` asks whether the PR's OWN head
  commit has a green gating CI check, something `validate_tripwires.
  check_ci_green()` never checked (that function only asks whether the BASE
  branch is red). Previously nothing stopped a validator PASS being recorded
  for a PR whose own CI never ran or is actively failing — that was only
  caught later, at MERGE time, by `autonomous_merge._merge_readiness()`. This
  check reuses that exact classifier (`autonomous_merge.pr_state()`, a public
  wrapper added around the previously-private `_pr_state()` for this reuse)
  so the validate-time and merge-time views of "is CI green" can never
  disagree — it only surfaces the gap earlier, at validate time. A failing
  gating check is a blocking "high" finding; no green gating check YET
  (CI may simply still be running) is a visible, non-blocking "medium".

Wired in at both integration points: `validate_tripwires.run()` calls
`check_wrong_repo()` when `expected_repo` is supplied, and `validate_pr.
validate()` calls `check_missing_ci()` and folds its findings into the same
deterministic-findings list that already drives the early-BLOCK and
fail-closed-override paths. **Fail-closed policy** throughout (mirrors
`validator_repo_guard`'s documented rule): an inconclusive check — network
error, unresolvable identity, no PR context — degrades to a non-blocking
"medium" finding, never a fabricated "high"/BLOCK against real work, and
never silent either.

```bash
# manual adversarial spot-check of an open PR
python3 machine-setup/mini-scripts/pr_pipeline/adversarial_review.py \
  --repo owner/repo --pr 123 --expected-repo owner/repo
python3 machine-setup/mini-scripts/tests/test_adversarial_review.py  # hermetic
```

## Hermes CI VM health and recovery (`pr_pipeline/ci_health_watch.py`)

`ci_health_watch.py` is the canonical Hermes-hosted monitor for the `hermes-ci`
OrbStack VM and its expected `hermes-*` GitHub runners. It is manifest-managed
with `ci_health_topology.json`, so install or verify through the PR-pipeline
reconciler rather than copying individual files:

```bash
ACTIVE_RELEASE="$(ssh mini 'readlink "$HOME/.hermes/runtime-current"')"
python3 machine-setup/mini-scripts/reconcile_pr_pipeline.py install \
  --host mini --source-commit <sha> \
  --runtime-python "$ACTIVE_RELEASE/venv/bin/python"
python3 machine-setup/mini-scripts/reconcile_pr_pipeline.py verify  --host mini --source-commit <sha>
```

The topology is deliberately declarative and fail-closed. It declares the VM
probe commands, managed start/stop/restart commands, the exact four-runner
fleet (including `colingreig/thermal` / `hermes-thermal`), the no-agent cadence
(`300` seconds), and desired resources: 4 CPU, 6144 MiB, and one shared job
slot. Duplicate, missing, or unknown runners are rejected.

Two scheduled recoveries are governed and capped at one rerun:

- `colingreig/jdmbuysell-v4` / `Dead-image monitor` retains managed-restart
  overlap recovery.
- `colingreig/thermal` / `E2E Functional` inspects the exact `e2e functional
  suite (advisory)` job, even when job-level `continue-on-error` made the
  workflow look green. It reruns only a completed failed job on
  `hermes-thermal` when every completed step is `success` or `skipped` and at
  least one step is truly `pending`/`in_progress` with no conclusion. Any
  completed null, failure, cancellation, timeout, or action-required result
  fails closed.

For Thermal job-interruption recovery, only the newest matching scheduled run
is considered, it must be no older than 24 hours, and its head SHA is compared
with a freshly queried default-branch SHA both at classification and
immediately before dispatch. JDM retains its legacy managed-restart overlap
semantics and is not subjected to Thermal's current-main filter. At most one
rerun command is sent per poll. Later polls reconcile the new attempt and
persist its final job/run conclusion and observation time without ever
authorizing another dispatch.

Every recovery record is written before dispatch and includes run ID, job ID
when applicable, runner, classification, timestamps, dispatch result, and the
single attempt number.

The same manifest governs the Linux VM runner loop, systemd template, hook
scripts, and runner config. Installation configures OrbStack's desired 4
CPU/6144 MiB limits but does not restart the VM; apply them with the managed
idle-window lifecycle command below. Before an ephemeral runner work tree is
removed, the loop writes a mode-0700 archive containing `_diag`, available
run/job identifiers, a bounded unit journal excerpt, and cgroup/PSI resource
evidence. Archives retain seven days and at most twenty jobs per repository.
The acquire hook has one shared slot and fails closed after 30 minutes. Slot
files are private leases containing boot, systemd invocation, worker PID, and
timestamp evidence. A new runner cycle may remove only its own lease, and only
when a changed boot/invocation or dead worker proves it stale. The shared
acquire path may also remove another runner's lease when changed-boot or
dead-worker evidence proves that owner stale, but never from invocation
difference alone and never while its worker PID is live. Empty
registration-only churn creates no archive. A failed registration with
nonempty `_diag` is preserved in a separate private, seven-day/twenty-entry
failure bucket, so it cannot evict real job archives.

Use the managed lifecycle wrapper for any planned VM operation so the monitor
can distinguish planned maintenance from unknown-cause restarts:

```bash
python3 ~/.hermes/scripts/ci_health_watch.py lifecycle restart \
  --actor "colin" \
  --reason "apply governed 4 CPU / 6144 MiB Hermes CI resources in an idle window"
```

The wrapper appends intent evidence before it acts. Unmanaged restarts are not
guessed: lifecycle transition records keep `initiator=unknown` and
`reason=unknown` unless a recent managed intent exists.

On the first poll after this release, a legacy live state file containing both
known synthetic `boot-a` and run `111` test fixtures is copied mode 0600 into
`~/.hermes/state/ci-health/quarantine/`, then reinitialized. Lifecycle JSONL
evidence is preserved and receives a quarantine event. Unit tests always pass
an isolated state path and cannot trigger this production migration.

State and evidence locations on the Mini:

- `~/.hermes/scripts/.ci_health_state.json` stores alert dedupe, runner offline
  debounce, last VM transition, managed intent correlation, and original
  run-ID recovery attempts. It is written atomically.
- `~/.hermes/state/ci-health/lifecycle.jsonl` stores every observed VM boot-ID
  or availability transition with UTC timestamp, prior/current boot ID, host
  uptime, runner status summary, OrbStack command evidence, and managed or
  unknown initiator classification.
- `~/.hermes/state/ci-health/managed-lifecycle.jsonl` stores planned lifecycle
  actor/action/reason records before the OrbStack command is executed.

Controlled idle-window verification procedure:

1. Confirm the expected runner is online and no protected workflow is active.
2. Run the lifecycle wrapper with a concrete actor and reason.
3. Let the no-agent monitor poll on its managed five-minute cadence.
4. Confirm `lifecycle.jsonl` shows the boot transition with the managed
   initiator and that Slack received exactly one outage transition and one
   recovery notification if the runner went offline long enough to debounce.

Natural scheduled-run evidence to collect for the next `Dead-image monitor`
proof packet:

- GitHub run ID and URL.
- `event` value, expected `schedule`.
- `conclusion`.
- `runner_name` or runner evidence tying the run to `hermes-ci`.
- Runner recovery state from `.ci_health_state.json`.
- Matching lifecycle JSONL record proving whether the run overlapped a detected
  restart and whether the initiator was managed or explicitly unknown.

## Files

- `mini_health_attestation.py` — authoritative, machine-readable live Mini
  attestation. It binds the active release commit and cleanliness, launchd
  PIDs and loaded runtime files, content-addressed release receipts, config
  schema and migration proof, governed source/deployed assets, fenced
  executions, skill/credential preflights, and every enabled cron job. A
  rejected latest release attempt remains a visible warning while the newest
  valid activation matching `runtime-current` remains authoritative. Real
  research-stage threshold breaches remain failing checks. `--migrate-config`
  uses the ordinary noninteractive migration path with a private backup and
  content-addressed receipt; `--snapshot` evaluates deterministic fixtures.
- `reconcile_launchd_environment.py` — governed installer for
  itself, `gateway_secrets_wrap.sh`, `dashboard_secrets_wrap.sh`,
  `gateway_launch_inner.sh`, `github_app_token.py`, `op_sdk_resolve.py`, the
  source-controlled keys in `launchd-secrets.op-env.template`, and both
  generated LaunchAgent plists. It validates and retains the complete
  reference-only `op-secrets.env` inventory while overlaying those required
  keys into the governed `launchd-secrets.op-env`. It atomically replaces
  regular files, records exact
  content-addressed rollback snapshots and hash receipts, and rejects a
  `config.yaml` whose `gateway.launchd_secrets_wrapper` is not the canonical
  installed gateway wrapper. Plists point only to wrappers and use
  `KeepAlive.SuccessfulExit=false`: permanent authentication errors exit
  cleanly and park, while transient exhaustion remains nonzero/retryable.
- `reconcile_marketplace_skills.py` — governed installer for the canonical
  external-skill roots, `skills.index_floor`, the ignite/Anthropic pull
  wrappers, and their LaunchAgent plists. It retires only the exact legacy
  `com.ignite.skills-sync` plist/job, rejects missing or symlink-escaped roots,
  records source/deployed SHA-256 receipts, and snapshots config plus all owned
  paths for exact release rollback. Successful pulls write parseable UTC JSON
  evidence under `~/.hermes/state/skill-pulls/`; the verifier checks both jobs
  are loaded and each source remains within three scheduled cadences. Pulls
  fail before fetch/evidence when tracked or untracked prompt-visible content
  is dirty. Their directory locks carry PID + process-start ownership, reject
  live contention, and reclaim dead/reboot-stale owners without a permanent
  mkdir wedge. Changed pulls publish a catalog generation observed by the
  running gateway/dashboard; those processes clear only future-session and
  catalog caches, never an existing conversation's byte-stable system prompt.
- `github_app_token.py` — mints a short-lived GitHub App installation token.
  Both service wrappers invoke it with the absolute runtime Python, require a
  non-empty token, and never log the token or credentials. The gateway inner
  wrapper alone maps `OPENAI_API_KEY_HERMES` to `OPENAI_API_KEY` and exports
  the canonical LOW/MEDIUM/HIGH validator chain.
- `clickup_workspace_refresh.py` — canonical source for the Mini's protected
  `~/.hermes/scripts/clickup_workspace_refresh.py`. Unlike legacy manual-copy
  entries, this file is installed by `scripts/mini-release-cut.sh`: source and
  deployed SHA-256 values are recorded in a content-addressed release receipt,
  and rollback atomically restores the prior release's source or the staged
  pre-vendor bytes.
- `op_sdk_resolve.py` — resolves `op://` secret references for
  `gateway_secrets_wrap.sh` and cron/sentinel scripts. **Connect-first since
  2026-07-24**: prefers a locally-run 1Password Connect server (see
  `op-connect/`) and falls back to the cloud service-account SDK only when
  Connect is down or a ref is outside the Connect token's vault scope — this
  moves routine resolution off the rate-limited cloud account. Verified live:
  the gateway boot fetches all 142 secrets from both vaults through Connect
  (`127.0.0.1:8080`) with zero cloud calls.
  Restored 2026-07-21 with the HERMES-PATCH 31 resilience layer (cache,
  retry/backoff, serve-stale, id-fast-path) re-added from the original spec
  in ClickUp 86e2a99q9 after the 2026-07-19 loss; live-verified (142/142
  secrets resolved, cache hit confirmed on a second run, 0700/0600 perms).
  Hardened for ClickUp 86e2a2paz on 2026-07-23: auth/unauthorized/invalid/
  forbidden/expired/token markers take precedence over transient-looking text;
  transient failures use three bounded jittered retries around 5/15/45 seconds;
  exhausted transient failures serve stale only when every requested value has
  complete usable cache data, otherwise they retain the CLI's exact FATAL/exit-1
  contract. Importable `resolve_refs()` consumers now use the same retry/cache
  path, while stdout remains byte-compatible `KEY="value"` shell input.
- `sentinel_run.sh` — launchd runner for Ignite Sentinel. De-clusters its
  1Password resolve with a random delay in the inclusive 0–120 second window.
  `SENTINEL_START_DELAY_MAX_SECONDS=0 SENTINEL_SMOKE_ONLY=1` performs the
  secrets-resolution smoke without running the monitor or emitting Slack.
- `degraded_secrets_monitor.py` — detects repeated secret-resolution failures
  and unresolved placeholders. Its SDK subprocess uses the immutable
  `~/.hermes/runtime-current/venv/bin/python` path, not the removed mutable
  `~/.hermes/hermes-agent/venv/bin/python` checkout.
- `tests/test_op_sdk_resolve.py` — fully mocked resolver contract harness:
  transient-then-success, exhausted transient without stale, mixed auth +
  timeout precedence, complete stale fallback, and stdout quoting bytes.
- `tests/test_sentinel_run.sh` — validates the 0–120 second delay contract and
  executes a fake-HOME, Slack-silent sentinel smoke.
- `tests/test_op_sdk_consumers.sh` — verifies canonical/live resolver byte
  identity plus the sentinel, degraded-monitor, and marketplace consumer paths.
- `verify-hermes-patches.sh` — idempotent guard/health-check for the 12 legacy
  hand-patches (now all formally merged to main) plus ~30 other live-deploy
  sentinels (GH App token, marketplace sync cron, validator model chain,
  skills freshness, DB-publish lane, etc). Fixed 2026-07-22 (ClickUp 86e2e7z2h)
  to stop hardcoding the pre-2026-07-19 mutable `$HOME/.hermes/hermes-agent`
  checkout — `REPO` now resolves `$HOME/.hermes/runtime-current` (the current
  immutable release). Since the original `.patch` diff files were lost in the
  same 2026-07-19 wipe and are unrecoverable, patch verification is now
  sentinel-first (grep a load-bearing string in the live release) rather than
  `git apply --reverse --check` against a file that no longer exists; a `.patch`
  file, if one is ever added back to `~/.hermes/local-patches`, still gets the
  git-apply re-application path. Before this fix the script exited at `cd
  "$REPO"` before reaching ANY of its ~30 other checks — those were silently
  unverifiable since 2026-07-19, not merely "assumed green".
- `offbox_restic_backup.py` — nightly restic backup of `~/.hermes` to
  Cloudflare R2. `BACKUP_TARGETS` added `~/.hermes/memories` 2026-07-22
  (ClickUp 86e2e870p) after discovering it had never been in scope — the
  2026-07-19 wipe permanently lost Hermes's entire MEMORY.md/USER.md
  personalization with zero restic snapshot history to restore from, at any
  point. This closes the gap for future incidents; it does not recover what
  was already lost (see 86e2e870p for the reseed decision, separately pending
  Colin's input). Failure alerts are signature-deduplicated in
  `~/.hermes/state/offbox-backup-monitor.json`; the initial failure and
  recovery advance that state only after Hermes confirms Slack delivery, so
  an unavailable transport remains retryable.
- `tests/test_offbox_restic_backup.py` — verifies delivery-aware failure and
  recovery persistence through the real Hermes-send boundary.
- `research_exec.py` — bounded pre-write content research stage. It resolves
  `SCRAPINGBEE_API_KEY` through Hermes's in-memory lazy 1Password resolver,
  searches and fetches a small source set, and treats every fetched byte as
  untrusted data. Analysis uses a fixed direct Anthropic Messages API request:
  no tool declarations, tool choice, MCP connectors, container, filesystem,
  shell, browser, agent runtime, or action interpreter. The tool-capable
  `opencode_exec.py --dangerously-skip-permissions` path is explicitly forbidden
  for fetched content. Only text response blocks become the brief; tool-use
  blocks are ignored and produce a flag-and-ship fallback. Its independent
  `content_pipeline.research.enabled` switch in `~/.hermes/config.yaml`
  defaults on; any key/API/paywall/bot/analyzer failure is flag-and-ship and
  leaves the writer able to continue.
  A page that is thin without JavaScript may receive one JS-rendered retry,
  capped at two retries per run. Execution receipts expose
  `js_render_retries` and `grounded_pages_recovered_by_js` to measure the
  grounding gain attributable to those retries, without recording the API
  key, query text, fetched content, or generated brief.
- `research_stage_monitor.py` — independent served-ledger liveness check. It
  reports recent enabled attempts, successful serves, degraded attempts, and
  served rate. Exit codes: `0` healthy, disabled-or-smoke-only, or advisory
  `insufficient-data` (fewer than `--min-attempts` real, non-smoke attempts
  before the persistent-inconclusive threshold), `2` degraded (provider
  genuinely failing on real traffic), `3` not-observed (stage never ran /
  ledger missing or stale), and `5` fetch-degraded
  (`fetch_success_rate < 0.50`) — the JSON `status` field is authoritative,
  and the exit code exists for consumers that only check process exit status.
  The JSON also exposes
  `fetch_success_band` as `alarm`, `warn`, `healthy`, or `null`; the warning
  band is `0.50 <= fetch_success_rate < 0.70`. `--quiet-when-healthy`
  suppresses stdout (still exits 0) when status is `healthy` or
  `disabled-or-smoke-only`, except that a `warn` fetch band remains visible
  so the Mini cron/Slack delivery is actionable without changing status or
  exit codes. Every status other than quiet healthy/disabled still prints the
  full JSON, including advisory `insufficient-data`. Cross-run
  state is written atomically to
  `~/.hermes/state/research-stage-monitor.json`; one continuous
  `not-observed` / `insufficient-data` window escalates after more than 72
  hours to `status=persistently-inconclusive` with exit `6`. Moving between
  the two inconclusive statuses preserves the timer, while any conclusive
  status resets it.
- `research-stage-monitor-cron.py` — thin `no_agent` cron wrapper for the
  monitor above. Mini cron jobs can't pass script arguments (`argv` is
  hardcoded to `[interpreter, path]` in `cron/scheduler.py`), so this
  wrapper bakes in `--quiet-when-healthy` and calls
  `research_stage_monitor.py` as a sibling file (resolved via
  `Path(__file__).resolve().with_name(...)`, never a hardcoded absolute
  path). The cron job delivers every non-success status and also delivers a
  `status=healthy` payload when `fetch_success_band=warn`, plus the
  `persistently-inconclusive` escalation after more than 72 hours; genuinely
  healthy non-warning results remain silent. It propagates the monitor's
  stdout, stderr, and exit code verbatim; if the sibling script is missing it
  prints an error to stdout (so the cron job surfaces it) and exits `1`.
- `content-research-baseline.json` — phase-1 pre-rollout metrics snapshot,
  including the audited 1/3 content-gate execution rate and the historical
  0/29 Sonnet serve comparator, with unknown historical metrics explicitly
  marked uninstrumented rather than inferred.
- `research_outcome_metrics.py` — content-outcome instrumentation and report.
  Every successful `opencode_exec.py --content` run automatically writes a
  content-free receipt to `~/.hermes/logs/content-outcomes.jsonl`: piece count,
  unique explicit external Markdown/HTML citation-link count, mean links per
  piece, share of pieces with a citation, and a hash of the measured path set.
  Prose, URLs, and filenames are never logged. `report` joins the latest receipt
  to `research-served.jsonl` (`grounded_pages`, `severity`) and the validator
  verdict store by ClickUp task ID. It reports citation coverage and validator
  fail rate by severity, but remains `insufficient-data` until both degraded
  and healthy cohorts have five validator-observed tasks. The dated rollout
  baseline is in `reports/research-outcome-validity-2026-07-25.md`.
- `tests/test_research_stage.py` — verifies secret-safe auth, strict untrusted
  data boundaries, the analyzer request's zero-tool surface, refusal to
  interpret tool-use responses, bounded HTTP reads, flag-and-ship fallback,
  cannibalization context, content-free receipts, and monitor thresholds.
- `tests/test_research_outcome_metrics.py` — verifies citation counting, path
  containment, content-free receipts, task-ID joins, legacy-ledger handling,
  latest-verdict selection, cohort floors, and the rendered report.
- `claim_store.py`, `clickup-queue-poller-claim_next.py`, `opencode_exec.py` —
  the executor claim/dispatch chain (`claim_next.py` picks a candidate task and
  atomically locks it via `claim_store.py`; `opencode_exec.py` then runs the
  writer model against it). Vendored here 2026-07-24 (ClickUp 86e2ddcpb spend-
  guard trip postmortem) as the canonical git home for what had been mini-only,
  untracked files — see "Claim retry cap" below for the incident and the fix
  layered on top in the next commit.
- `tests/test_claim_history.py` — covers the per-task attempt-cap logic added
  on top of the vendored claim chain (see "Claim retry cap" below).
- `pr_pipeline/pr_staleness_alert.py` — Slack-delivered cron wrapper (mini job
  `pr-staleness-alert`, id `3043a00e6df8`) for open PRs stuck without a fresh
  validator verdict. Vendored 2026-07-24 (byte-identical, commit 1) then
  fixed (commit 2) for the 15-minute repost bug — see "PR staleness dedupe"
  below.
- `tests/test_pr_staleness_alert.py` — covers the dedupe fingerprint, decide
  logic (unchanged/new/dropped/bucket-crossing/heartbeat), and fail-open
  state loading, both as pure functions and end to end through `run()`.
- `pr_pipeline/` — canonical Mini PR-review, validation, tripwire, CI, risk,
  Slack, and merge-guard closure. `manifest.json` resolves its source hashes
  at deployment time, including every `pr_pipeline/*.py` trust-boundary
  component. Remaining legacy flat entry points and the package namespace are
  generated Mini artifacts from these sources.
- `reconcile_pr_pipeline.py` — the only deploy/verify path for that closure.
  It installs only manifest paths, records the supplying source commit plus
  every deployed SHA-256 in `.pr_pipeline_deployment.json`, and reports
  missing, extra, or drifted pipeline files. Source identity is exact (a full
  SHA equal to the active release), never ancestor-only. Reconciliation also
  runs the deployed root `review_poll_gate.py` command from `/` with the
  release Python and a sanitized environment, then records the smoke and a
  composite reconciliation receipt. The snapshot is hard-shadowed: this
  deployment surface never enables or invokes a live merge.
- `github_app_cred.sh` — git credential helper for the Hermes Dev Assistant
  GitHub App, wired up by `~/.hermes/gitconfig` (`GIT_CONFIG_GLOBAL`). Mints a
  short-lived installation token per request via `op-run` +
  `github_app_token.py`; never stores one.
- `hermes-bin-gh` — **deploys to `~/.hermes/bin/gh`, not `~/.hermes/scripts/`.**
  The `gh` wrapper that exports a freshly-minted `GH_TOKEN` before exec'ing the
  real Homebrew `gh`.
- `dot-profile` — **deploys to `~/.profile`.** Sourced by every `bash -l` the
  terminal tool spawns for its session-env snapshot; hoists `~/.hermes/bin`
  back to the front of `PATH` after `/etc/profile`'s `path_helper` demotes it.
  It also exports `GIT_CONFIG_GLOBAL=~/.hermes/gitconfig` when that file
  exists and the caller has not explicitly set `GIT_CONFIG_GLOBAL` or
  `GIT_CONFIG_NOGLOBAL`, so default non-interactive mini Git commands can use
  the GitHub App credential helper.
- `spend_guard.py` — hard $50/day spend cap gating every `opencode_exec.py`
  delegation. Vendored 2026-07-26 as the canonical git home for what had been
  a mini-only, untracked file; the live file already carried its fix, so
  there is no separate vendor-verbatim base commit — see the module docstring
  for the full before/after. Summary of the defect it fixes: `is_over_cap()`
  used to catch every state.db/opencode-log read error and return `False`
  ("not over cap"), identical to a genuinely healthy $0 day — the cap
  silently stopped enforcing on any read hiccup. Fixed with a three-tier
  policy: a clean read behaves exactly as before; a read failure within a
  recent last-known-good window (`HERMES_SPEND_GUARD_STALENESS_SECONDS`,
  default 900s) alarms loudly and decides from that cached figure; a read
  failure with no usable cache alarms loudly and fails closed (blocks new
  spend) rather than fail open indefinitely.
- `spend_meter.py` — per-provider ($/provider/day) companion to
  `spend_guard.py`'s global cap. Vendored 2026-07-26, live file already
  fixed, same "no separate base commit" note as above. Fixes: a state.db
  read failure used to be swallowed (`except Exception: return {}`), making
  `is_over_threshold()` return `[]` — indistinguishable from "checked,
  everyone's under cap." A read failure now raises `SpendDataUnavailable`,
  which `main()` turns into a non-zero exit plus a loud message so the
  `spend-meter` cron job's failure-delivery path actually fires instead of
  going silent. This meter has no blocking power (unlike `spend_guard.py`),
  so alarming loudly on "can't check" is the correct and sufficient fix.
- `hermes_usage_alert.py` — zero-LLM Slack alarm for provider/credential
  exhaustion, fallback-chain exhaustion, and cron jobs stuck or freshly
  entered into an error `last_status`. Vendored 2026-07-26, live file already
  fixed, same "no separate base commit" note as above. Two fixes: (RC1)
  `_scan_cron_errors` used to alert only on a transition into error and
  explicitly skip a job's first-ever observation, so a persistently-red job
  alerted once and then went silent forever, and a state-file reset
  re-silenced every already-red job by making "first observation" look like
  "nothing to report." A job's first observed state is now itself
  alert-worthy if it's `error`, and a standing-red job re-alerts on a bounded
  cadence (`HERMES_CRON_ERROR_REALERT_MIN`, default 360min). (RC2)
  `_scan_cron_errors` used to catch any `jobs.json` read/parse failure and
  return `[]` — indistinguishable from "read fine, no errors." An unreadable
  `jobs.json` now produces its own distinct `monitor_error` alert instead of
  silently reporting all-clear. Task `86e2jbbhx` also closes the delivery
  loop: the script now sends its own combined alert through
  `hermes send --to slack:hermes` and persists offsets/cooldowns only after a
  confirmed send. It no longer depends on the six-hour LLM digest noticing
  stdout, and a Slack outage cannot advance dedupe into silence.
- `fleet_outcome_probe.py` + `fleet_outcome_contracts.json` (task
  `86e2jbbhx`) — independent zero-LLM coverage plane for all 16 rebuilt cron
  jobs and every Hermes/Ignite LaunchAgent. It requires a fresh semantic
  output, endpoint, or durable receipt in addition to scheduler/launchd state;
  fails closed on uncovered enabled jobs and monitored plists; and sends
  delivery-aware alarms directly through `hermes send --to slack:hermes`.
  LaunchAgent
  `launchd/com.colingreig.hermes.fleet-outcome-probe.plist` runs it every five
  minutes. `ci-health-watch-cron.py` cross-watches the probe's receipt, while
  the probe verifies CI health's fresh parsed state (stable lifecycle, VM
  available, no resource drift), so neither monitor depends on its own
  scheduling plane. The full inventory and outcome mapping is in
  `../fleet-config/MONITOR_COVERAGE.md`.

  Deployment is governed by the content-addressed
  `fleet_outcome_manifest.json` and transactional
  `reconcile_fleet_outcomes.py`. The normal Mini release cut invokes it
  automatically. For an explicit verification or repair from an exact release:

  ```bash
  ~/.hermes/runtime-current/venv/bin/python \
    ~/.hermes/runtime-current/machine-setup/mini-scripts/reconcile_fleet_outcomes.py \
    verify \
    --source-root ~/.hermes/runtime-current/machine-setup/mini-scripts \
    --manifest ~/.hermes/runtime-current/machine-setup/mini-scripts/fleet_outcome_manifest.json \
    --reload
  ```
- `hermes_report_build.py` fix (2026-07-26): the status-email spend section
  rendered a served-ledger read failure the same as a genuine $0.00 day —
  `spend['total_cost']` stayed `None` on error but every formatter still ran
  `f"${spend['total_cost']:.2f}"`. Added `_cost_display()` plus an explicit
  `spend['error']` field threaded through the subject line, headline, HTML
  render, text render, and JSON summary so an unreadable ledger renders as
  "spend UNKNOWN (ledger unreadable)" and never as a silent zero.
- `mcp_serve_reaper.py` (task 86e2hap4g) — standalone, redundant, age-based
  safety net (same philosophy as `worktree_backstop_sweep.py`) that reaps
  orphaned per-session `hermes mcp serve` stdio subprocesses left behind by a
  dropped SSH/MCP client. Liveness-based, NOT release-path-based: a
  release-cut-tied reaper would kill healthy in-progress Claude Code/Codex
  sessions, since "release path != current" mostly selects processes started
  before the last deploy. Reaps only when a process is both genuinely
  orphaned (`ppid == 1`, or the parent chain never reaches a live
  `sshd-session:` ancestor before PID 1) AND older than `--min-age-minutes`
  (default 45, avoids flip-timing races) — TERM, grace period, then KILL.
  LaunchAgent `com.colingreig.hermes.mcp-serve-reaper` runs it every 20 min
  (`launchd/com.colingreig.hermes.mcp-serve-reaper.plist`). Sibling fix folded
  into the same task: `scripts/mini-release-poll.sh` now passes `--prune` to
  `mini-release-cut.sh` (previously never pruned, so old release dirs
  accumulated unbounded — `mini-release-cut.sh` already had `--prune` wired
  and tested, it just wasn't being invoked).
  Task `86e2jbbhx` removes its false-green exit: a failed `ps` snapshot now
  exits 2, and any selected process that cannot be reaped makes the sweep exit
  1. An empty readable snapshot and a clean no-op sweep remain successful.
- `wt-new` (ClickUp 86e2evnx0) — Python3 CLI that creates a per-task worktree
  off the shared bare mirror (`git worktree add`), resolving the mirror's
  default ref from `refs/remotes/origin/HEAD` when present, falling back to
  the mirror's own `HEAD` symbolic ref (typically `refs/heads/main` on a true
  `--mirror` clone), and failing clearly (`--base` hint) when neither
  resolves. Its Git subprocess environment now defaults
  `GIT_TERMINAL_PROMPT=0` and, when the caller has not supplied an explicit
  Git config mode, injects `GIT_CONFIG_GLOBAL=~/.hermes/gitconfig` if that
  file exists. That makes the GitHub App credential helper available to the
  default `git remote update` path used by `wt-new` and callers such as
  repo-baseline tooling without every caller remembering to export the config.
  Was previously live-only at `~/.hermes/scripts/wt-new` with no repo history;
  vendored here verbatim (byte-for-byte, sha256-matched) so future changes are
  reviewable and revertible through source control like every other
  manual-copy script. Deploy by scp-by-name, together with the profile hook
  that makes the default login-shell Git config available:
  `scp machine-setup/mini-scripts/wt-new mini:~/.hermes/scripts/wt-new && scp machine-setup/mini-scripts/dot-profile mini:~/.profile`.
  Live post-copy smoke:
  `ssh mini 'chmod 755 ~/.hermes/scripts/wt-new && chmod 644 ~/.profile && bash -lc '"'"'test "${GIT_CONFIG_GLOBAL:-}" = "$HOME/.hermes/gitconfig" && GIT_TERMINAL_PROMPT=0 git ls-remote https://github.com/colingreig/ignite-skills.git HEAD >/dev/null'"'"''`.
- `tests/test_wt_new_default_ref.py` — covers `default_ref()`'s three cases:
  true mirror falling back to `refs/heads/main`, an ordinary clone with
  `origin/HEAD` set, and a bare repo with neither ref resolving cleanly to a
  `SystemExit` with the `--base` hint. It also covers the mini Git credential
  environment contract and verifies `try_fetch()` passes that environment to
  its subprocess Git invocation.
- `tests/test_dot_profile_env.py` — covers the mini login-shell profile hook:
  `~/.hermes/bin` is re-hoisted, `~/.hermes/gitconfig` becomes the default
  global Git config when present, and explicit caller Git config overrides are
  preserved.

## Cron-context GitHub auth (2026-07-26)

Symptom: every executor cron session logged
`no oauth token found for github.com`; zero autonomous branches/PRs for ~2
days, while interactive sessions worked fine.

Root cause — one PATH fault with two independent effects. The terminal tool
builds its session env from a `bash -l` login shell
(`tools/environments/local.py::_resolve_shell_init_files`). `/etc/profile`
runs `/usr/libexec/path_helper`, which rebuilds `PATH` as
`/etc/paths` + `/etc/paths.d/*` first and appends the inherited entries after,
so the gateway's leading `~/.hermes/bin` and release `venv/bin` were both
demoted below `/usr/local/bin` and `/opt/homebrew/bin`. Therefore:

1. `gh` resolved to `/opt/homebrew/bin/gh` — the real, unauthenticated CLI —
   instead of the App-token wrapper.
2. Even when the wrapper *was* invoked by absolute path, its bare `python3`
   resolved to the python.org `/usr/local/bin/python3` 3.13 build, which has no
   CA bundle: `URLError(SSL: CERTIFICATE_VERIFY_FAILED … unable to get local
   issuer certificate)` against `api.github.com`. `2>/dev/null || true`
   swallowed it, so `GH_TOKEN` was silently empty. `github_app_cred.sh` hit the
   same interpreter fault and returned empty credentials, breaking `git push`
   over HTTPS as well.

Fix:

- `dot-profile` re-hoists `~/.hermes/bin` to the front of `PATH` (removing any
  demoted occurrence first, so a present-but-late entry is actually promoted).
- `hermes-bin-gh` and `github_app_cred.sh` resolve the interpreter absolutely
  (`~/.hermes/runtime-current/venv/bin/python`, falling back to
  `/usr/bin/python3`) instead of trusting `PATH`.
- Both log mint failures to `~/.hermes/logs/github-app-token.log` rather than
  discarding stderr — the silent swallow is what hid this for two days.

## Claim retry cap (ClickUp 86e2ddcpb, 2026-07-24)

Root cause: `claim_next.py` fails open on any error (by design — a claim-store
bug must never block legitimate work) and had NO cap on how many times the
same task could be reclaimed. Two tasks (`86e2dda93`, `86e2cpdgh`) looped
12 and ~6 sessions respectively, each one ending "opencode finished but made
no file changes" / "no commit or push" (never reaching a PR), tripping the
$50/day spend guard and blocking all executor work for the rest of the day.

Fix (implemented across `claim_store.py` + `clickup-queue-poller-claim_next.py`
+ `opencode_exec.py`):
- `opencode_exec.py` calls `claim_store.record_attempt(task_id, outcome, note)`
  at session end (`success` / `fail` / `crash`), appending to a per-task
  history file at `~/.hermes/state/claim_history/<task_id>.json` (list
  capped at `HERMES_CLAIM_HISTORY_MAX_ENTRIES`, default 50).
- `claim_next.py`'s per-candidate loop now excludes any task with MORE than
  `HERMES_CLAIM_MAX_ATTEMPTS` (default 5) recorded attempts in the trailing
  24h (`COOLDOWN_SECONDS`) window — counted by **task id**, not claim run, so
  a reclaim of a still-fresh task never double-counts. An excluded task has
  its `agent-ready` tag stripped, gets tagged `attempt-cap-exceeded-<ts>`, and
  gets a ClickUp comment with the attempt count and last recorded failure.
- Every new code path fails OPEN: a missing/corrupt/unreadable history file,
  or any other error, is treated as "under cap" and the claim proceeds
  normally. A bug in the cap logic must never block the queue — this mirrors
  the existing `claim_store.py` acquire/release fail-open contract.

Deploy (copy commands — run from the repo root; `mac-mini-h.tail51ec1b.ts.net`
is the mini's Tailscale name):

```bash
scp machine-setup/mini-scripts/claim_store.py \
    mac-mini-h.tail51ec1b.ts.net:~/.hermes/scripts/claim_store.py
scp machine-setup/mini-scripts/opencode_exec.py \
    mac-mini-h.tail51ec1b.ts.net:~/.hermes/scripts/opencode_exec.py
scp machine-setup/mini-scripts/clickup-queue-poller-claim_next.py \
    mac-mini-h.tail51ec1b.ts.net:~/.hermes/skills/clickup-queue-poller/scripts/claim_next.py
mini-run -- 'python3 -m py_compile ~/.hermes/scripts/claim_store.py \
    ~/.hermes/scripts/opencode_exec.py \
    ~/.hermes/skills/clickup-queue-poller/scripts/claim_next.py'  # sanity check
```

## PR staleness dedupe (mini cron job pr-staleness-alert, 2026-07-24)

Root cause: `pr_pipeline_improvements.check_staleness_and_alert()` (then a
separate Mini-only module, now included in the canonical PR-pipeline package)
fingerprinted each stale PR on `round(age_hours, 2)` — a value that changes on almost every
15-minute tick — so its `.pr_pipeline_state.json` comparison never matched
two runs in a row. Confirmed byte-identical Slack payload across six
consecutive runs (20:02-21:16 on 2026-07-23), 257 runs since 2026-07-22, for
the same 6 unresolved stale PRs. The signal was real; the dedupe granularity
was the defect.

Fix: `pr_pipeline/pr_staleness_alert.py` no longer calls that function at all. It calls
the lower-level scan/staleness primitives (`GitHubClient`, `scan_repos`,
`stale_without_verdict`, `utcnow`, `notify` — all re-exported by
`pr_pipeline_improvements`) directly, then owns its own dedupe:
- Fingerprints the current stale set on `(repo, PR number) -> coarse age
  bucket` (`PR_STALENESS_AGE_BUCKET_HOURS`, default `24,72,168`), persisted
  at `PR_STALENESS_STATE_PATH` (default `~/.hermes/state/pr_staleness_last.json`
  — a new path, independent of the old broken `.pr_pipeline_state.json`).
- Posts to Slack only when that fingerprint changes (a PR enters/leaves the
  stale set, or crosses a bucket boundary), plus a heartbeat at most once
  per `PR_STALENESS_DIGEST_HOURS` (default `24`) so an
  unchanged-but-still-broken state resurfaces daily instead of going quiet
  forever.
- Fails OPEN on state-file errors: a missing/corrupt/wrong-shaped
  `pr_staleness_last.json` is treated as "no prior state," so the current
  stale set looks new and gets posted rather than silently suppressed —
  same fail-open contract as `claim_store.py`.

A separate agent applied an immediate cadence reduction on the mini cron
schedule (`*/15` -> daily) as a stopgap while this fix was in flight; that
cadence change is cron-config only and lives on the mini, not in this repo.

Deploy (run from the repo root; `mac-mini-h.tail51ec1b.ts.net` is the
mini's Tailscale name):

```bash
scp machine-setup/mini-scripts/pr_pipeline/pr_staleness_alert.py \
    mac-mini-h.tail51ec1b.ts.net:~/.hermes/scripts/pr_pipeline/pr_staleness_alert.py
mini-run -- 'python3 -m py_compile ~/.hermes/scripts/pr_pipeline/pr_staleness_alert.py'  # sanity check
```

## PR-pipeline source and deployment integrity

`machine-setup/mini-scripts/pr_pipeline/` is now the only authoritative
source for the Mini PR-review/merge closure. The Mini copies at
`~/.hermes/scripts/` and `~/.hermes/scripts/pr_pipeline/` are generated
artifacts. The manifest includes the remaining legacy flat entry points and
every Python file in the package, so a newly added trust-boundary module cannot
silently remain Mini-only.

The focused verifier/reconciler is deliberately separate from the legacy
patch checker: it stages the manifest sources to the Mini, writes only the
manifest destinations and deployment marker, then checks SHA-256 parity and
the recorded source commit. Supply the exact already-approved source commit;
the tool never derives it from, or mutates, a Mini checkout.

```bash
ACTIVE_RELEASE="$(ssh mini 'readlink "$HOME/.hermes/runtime-current"')"
python3 machine-setup/mini-scripts/reconcile_pr_pipeline.py reconcile \
  --host mini --source-commit <approved-source-commit> \
  --runtime-python "$ACTIVE_RELEASE/venv/bin/python"

python3 machine-setup/mini-scripts/reconcile_pr_pipeline.py verify \
  --host mini --source-commit <approved-source-commit>
```

Both commands preserve shadow mode. They do not invoke `gh`, a merge command,
or any PR pipeline cron; promotion to live merge behavior is a separate
reviewed change. `verify` is read-only on the deployed scripts (apart from its
temporary, removed source staging directory) and fails on a missing file,
hash mismatch, recorded-commit mismatch, or unmanifested pipeline extra.

**WARNING — do NOT rsync `~/.hermes/scripts/` wholesale.** That directory is
hand-maintained on the mini and holds ~203 live-only scripts with no git
backing (see the top of this file). Only ever `scp` the exact file(s) named
in the deploy command for the section you're working from (e.g. just
`claim_store.py`, `opencode_exec.py`, and `clickup-queue-poller-claim_next.py`
for "Claim retry cap" above, or just `pr_pipeline/pr_staleness_alert.py` for "PR
staleness dedupe" above) — never the directory. The same caution applies to
`~/.hermes/skills/clickup-queue-poller/scripts/` — copy only `claim_next.py`,
never the directory.

## hermes-self-report bundle (ClickUp 86e2gnz60)

The `hermes-self-report` cron builds and delivers Colin's status email. Its
seven artifacts now deploy through one **declared bundle + manifest-verified
installer** rather than ad-hoc `scp`. This directory is the deploy mirror; the
authored source of truth is Brain (`hermes/skills/hermes-self-report/`), which
mirrors these exact bytes and does not deploy anything itself.

**The bundle** (`self_report_manifest.json`) records `src_rel`, `src_base`,
`dest_abs`, `sha256`, `role`, and `deploy_mode` for each file:

| File | dest | canon |
|---|---|---|
| `hermes_report_build.py` | `~/.hermes/scripts/` | live RC4 |
| `hermes_report_build_v1lib.py` | `~/.hermes/scripts/` | live (load-bearing library) |
| `hermes_report_build_v2.py` | `~/.hermes/scripts/` | live (compat-only orphan, retirement pending) |
| `hermes-metrics.py` | `~/.hermes/scripts/` | live (standalone CLI, no cron caller) |
| `postmark_send_report.py` | `~/.hermes/scripts/` | deploy-mirror bytes (live behavior + `encoding="utf-8"`) |
| `hermes_self_report_delivery_probe.py` | `~/.hermes/scripts/` | deploy-mirror bytes (live behavior + `encoding="utf-8"`) |
| `SKILL.md` | `~/.hermes/skills/hermes-self-report/` | live RC4 (Brain source; installed only with `--include-skill --brain-path`) |

**The installer** (`install_self_report.py`, stdlib only, `no_agent`-safe) is
the **sole writer** of these files. It:

- verifies every source's sha256 against the manifest and **fails closed** on
  drift (never installs unverified bytes);
- snapshots each existing destination into
  `~/.hermes/logs/self-report-installs/<UTC-ts>/` (plus a
  `<dest>.bak-self-report-install-<ts>` sibling) before writing;
- installs atomically (`write .tmp` → `os.replace`) and **re-reads the deployed
  bytes**, restoring the snapshot and exiting nonzero on any deployed-vs-manifest
  mismatch;
- writes a durable `install-receipt.json` recording every file's src/dest,
  expected/deployed sha, snapshot location, and overall result;
- refuses any destination outside `~/.hermes/` or named `claim_store.py` /
  `queue_snapshot.json`, and **warns (never installs)** if the required co-exist
  files `claim_store.py` / `queue_snapshot.json` are absent — the builder's
  work-stoppage verdict imports `claim_store` and degrades to UNKNOWN without it.

```bash
# preview: verify all source hashes, print the plan, write nothing
python3 machine-setup/mini-scripts/install_self_report.py --dry-run

# scripts only (default home = ~)
python3 machine-setup/mini-scripts/install_self_report.py

# scripts + SKILL.md from a Brain checkout
python3 machine-setup/mini-scripts/install_self_report.py \
  --include-skill --brain-path /path/to/brain-checkout
```

**Never** rsync `~/.hermes/scripts/` (see the wholesale-rsync warning above):
this installer copies only the seven declared files by name and touches nothing
else — in particular it never writes `claim_store.py`, `queue_snapshot.json`, or
the release venv. Because release/reconcile passes are known to clobber hand
edits, this manifest + installer must be the ONLY writer of these seven files
going forward.

**Delivery-probe cron.** `hermes_self_report_delivery_probe.py` runs as its own
`no_agent` cron job (offset from the `0 */6 * * *` report tick). It reads the
receipt `~/.hermes/logs/hermes-self-report-last-send.json` and exits `0`
(fresh + sent), `1` (both channels failed), or `2` (missing / stale / unreadable)
— no LLM, no send. The postmark sender's fallback target `slack:D0BA2PM9CFM` is
a Slack DM channel id (not a secret); `hermes send --list` on the mini is the
source of truth if it rotates.

## hermes-spend-guard bundle (ClickUp 86e2hdxcb)

Before this bundle existed, `spend_opencode.py`, `opencode_exec.py`,
`spend_guard.py`, and `spend_meter.py` had no manifest and no governed
installer at all — a release could change any of them and
`scripts/mini-release-cut.sh` would still print "governed script deployment
verified," because that check only ever covered
`clickup_workspace_refresh.py`. The merged 86e2hap1g billing-route fix shipped
as v0.18.2 and never reached the live `~/.hermes/scripts/` copy the mini cron
actually executes (see `hermes-cron-executes-from-scripts-dir-not-release`).
This bundle closes that gap the same way `self_report_manifest.json` +
`install_self_report.py` already closed it for the status-email chain.

**The bundle** (`spend_manifest.json`) records `src_rel`, `src_base`,
`dest_abs`, `sha256`, `role`, and `deploy_mode` for each file:

| File | dest | role |
|---|---|---|
| `spend_opencode.py` | `~/.hermes/scripts/` | shared billing-route helpers imported by the other three |
| `opencode_exec.py` | `~/.hermes/scripts/` | executor's code-writing delegate (STEP 4 of clickup-queue-poller/SKILL.md) |
| `spend_guard.py` | `~/.hermes/scripts/` | hard $50/day spend cap gating every delegation |
| `spend_meter.py` | `~/.hermes/scripts/` | per-provider ($/provider/day) companion meter, no blocking power |

**The installer** (`install_spend.py`, stdlib only, `no_agent`-safe) is the
**sole writer** of these four files. It mirrors `install_self_report.py`
exactly: sha-pinned, fails closed on any source hash drift, snapshots each
existing destination into `~/.hermes/logs/spend-guard-installs/<UTC-ts>/`
(plus a `<dest>.bak-spend-install-<ts>` sibling) before writing, installs
atomically and re-verifies the deployed bytes (restoring the snapshot on any
mismatch), writes a durable `install-receipt.json`, and refuses any
destination outside `~/.hermes/` or named `claim_store.py` /
`hermes_report_build.py`. It **warns (never installs)** if those two required
co-exist files are absent: `opencode_exec.py` dynamically loads
`claim_store.py` from its own directory to record the per-task outcome, and
`hermes_report_build.py` (owned by the hermes-self-report bundle) imports
`spend_opencode.served_row_cost` to strip phantom Codex-OAuth spend from
legacy rows.

```bash
# preview: verify all source hashes, print the plan, write nothing
python3 machine-setup/mini-scripts/install_spend.py --dry-run

# scripts (default home = ~)
python3 machine-setup/mini-scripts/install_spend.py
```

**Never** rsync `~/.hermes/scripts/`: this installer copies only the four
declared files by name and touches nothing else. Because release/reconcile
passes are known to clobber hand edits, this manifest + installer must be the
ONLY writer of these four files going forward.

`scripts/mini-release-cut.sh`'s post-cut receipt now scans every file that
changed in the cut release under `machine-setup/mini-scripts/` and warns by
name if any changed file is not covered by `self_report_manifest.json`,
`spend_manifest.json`, `pr_pipeline/manifest.json`, or the three files it
vendors directly (`clickup_workspace_refresh.py`,
`reconcile_launchd_environment.py`, `reconcile_marketplace_skills.py`) —
so an uncovered change is flagged instead of silently rolling up into
"governed script deployment verified." Neither `install_self_report.py` nor
`install_spend.py` is invoked automatically by the cut itself (they require an
explicit, deliberate run — see each installer's usage above); the drift check
only makes the receipt honest about what did and did not deploy.

## Disk lifecycle (worktrees / kanban / releases, ClickUp 86e2k3ryc)

Fix for the mini's 100%-disk incidents: ~101GB across 135 per-task worktrees,
~26GB across 30 kanban scratch workspaces, and ~11GB across 5 un-pruned
release dirs, none of which had a tight-enough automated retention policy or
a trailing disk-free alarm. Four independent pieces, each safe standalone:

- **`scripts/worktree_backstop_sweep.py` tuning** — unchanged safety model
  (dirty/ahead/no-remote/claimed/deliverable checks still fully fail-closed;
  never weakened), two additions: (1) `--min-free-gb`/`--pressure-days`
  (env `HERMES_WORKTREE_BACKSTOP_MIN_FREE_GB` /
  `HERMES_WORKTREE_BACKSTOP_PRESSURE_DAYS`) make the AGE gate
  disk-pressure-adaptive — once free space on `--root`'s filesystem drops to
  or below `--min-free-gb`, the effective age threshold tightens to
  `--pressure-days` (default 2) instead of the normal `--days` (default 7),
  so the backstop reclaims eligible space faster exactly when it matters
  most, without ever touching what the dirty/ahead/claim checks protect. A
  `disk_usage()` failure fails toward the NORMAL threshold, never the
  tighter one. (2) every removed/would-remove candidate now logs its size
  (`du -sk`, reporting-only, never a deletion input) and the sweep-finish
  summary reports `removed_bytes`/`removed_size`, so an operator can see
  where the bytes actually are instead of only skip-reason counts.
- **`kanban_workspace_sweep.py` (new)** — per-board, age-based backstop for
  kanban scratch workspaces, same philosophy as the worktree backstop above.
  `hermes kanban gc` already reclaims scratch workspaces for `archived`
  tasks, but only for whichever ONE board `get_current_board()` resolves to
  in its process env, has no `--board` flag, and isn't wired to any
  scheduler in this repo — a multi-board swarm (5 profiles live) can
  silently accumulate `done`-but-never-archived workspaces board by board
  indefinitely. This script is standalone (no hermes package import, same
  design constraint as the worktree backstop so it survives launchd's
  minimal env): it discovers every board's `kanban.db` +
  `workspaces/` directory pair itself (the default board's
  `kanban/workspaces/`, plus `kanban/boards/<slug>/workspaces/` for every
  board dir present — mirrors `hermes_cli/kanban_db.py`'s
  `_managed_scratch_path_info` root-enumeration convention), opens each
  board's DB read-only, and removes a candidate only when: it is NOT a
  symlink, the owning task's `workspace_kind` is `scratch` (never `dir`/
  `worktree`), any `workspace_path` override resolves to that same
  directory, the task's status is terminal (`done`/`archived` — never
  `triage`/`todo`/`scheduled`/`ready`/`running`/`blocked`/`review`), and it
  is older than `--days` (default 14). A directory whose id has no matching
  task row at all (orphan) is swept by age alone. If a board's DB can't be
  opened, that WHOLE board is skipped this run (fail closed) rather than
  guessing from disk state. `--dry-run` supported; every decision is logged
  with size (`du -sk`, reporting-only).
  ```bash
  python3 machine-setup/mini-scripts/kanban_workspace_sweep.py --dry-run --days 14
  ```
- **Release retention (`scripts/mini-release-cut.sh` `--prune`)** — standing
  policy is now "keep the active (`runtime-current` target) + previous
  release, prune everything else," replacing the previous
  `KEEP_RELEASES=3` default. That old default actually kept **4** releases
  (active + previous + 2 extra) because the prune loop only starts removing
  once `kept >= KEEP_RELEASES`, i.e. it keeps `KEEP_RELEASES - 1` extras
  before deleting the next one — an off-by-one baked into the variable's
  name. `KEEP_RELEASES_EXTRA` (env `MINI_RELEASE_KEEP_EXTRA`, default `0`)
  now names the ADDITIONAL count precisely: 0 means exactly 2 releases
  survive a prune, matching the standing policy; set it explicitly for a
  one-off wider retention window. Active and previous are still never
  removed regardless of this value — unchanged from before.
- **`disk_space_alert.py` (new)** — trailing safety net: retention alone is
  a leading indicator and only helps if its age thresholds are tight enough
  for whatever growth actually happens. This is a cheap, zero-LLM, cron-
  friendly direct disk-free check (same design as `hermes_usage_alert.py`:
  `hermes send --to slack:hermes`, JSON state file for cooldown, JSON
  receipt file for external health checks) that alerts on Slack the moment
  free space on the Hermes home's filesystem drops to or below
  `HERMES_DISK_ALERT_MIN_FREE_GB` (default `5` GB). Stays silent while
  healthy, re-alerts every `HERMES_DISK_ALERT_COOLDOWN_MIN` (default 60)
  while still low rather than going silent after the first ping, and — the
  `hermes-silent-monitor-failure-pattern` / `degraded-flag-unsatisfiable-
  alarm` lessons applied here — treats a failed disk-free check itself as a
  distinct, non-silent, non-zero-exit alert condition (own longer cooldown,
  `HERMES_DISK_ALERT_ERROR_COOLDOWN_MIN` default 360) instead of ever
  reading "couldn't check" as "healthy."
  ```bash
  python3 machine-setup/mini-scripts/disk_space_alert.py  # dry: HERMES_DISK_ALERT_DISABLE=1
  ```

Deploy all four by scp-by-name (manual-copy convention, same as every other
script in this section):
```bash
scp machine-setup/mini-scripts/kanban_workspace_sweep.py mini:~/.hermes/scripts/kanban_workspace_sweep.py
scp machine-setup/mini-scripts/disk_space_alert.py mini:~/.hermes/scripts/disk_space_alert.py
scp scripts/worktree_backstop_sweep.py mini:~/.hermes/scripts/worktree_backstop_sweep.py
scp machine-setup/mini-scripts/launchd/com.colingreig.hermes.{disk-space-alert,kanban-workspace-sweep,worktree-backstop-sweep}.plist \
  mini:~/Library/LaunchAgents/
```
`disk_space_alert.py` needs `slack_msg_builder.py` (already deployed flat in
`~/.hermes/scripts/` by the `pr_pipeline` bundle for `hermes_usage_alert.py`)
present as a sibling file — no separate action needed if the usage-alert
monitor is already live. New LaunchAgents load via `launchctl bootstrap
gui/501 ~/Library/LaunchAgents/<label>.plist` (see the gateway-restart notes
above for the bootout/bootstrap EIO-race caveat if reloading an existing one).

Tests: `machine-setup/mini-scripts/tests/test_kanban_workspace_sweep.py`,
`machine-setup/mini-scripts/tests/test_disk_space_alert.py`, and the pressure/
size-reporting additions in `tests/scripts/test_worktree_backstop_sweep.py`.
