# Mini-local scripts (canonical copies)

These scripts run on the Mac mini at `~/.hermes/scripts/` but live **outside**
the mini's git-tracked release/runtime-current deploy path (see
`hermes_cli/gateway.py` / `hermes update`) — nothing in this repo's deploy
pipeline provisions, copies, or regenerates `~/.hermes/scripts/*`. That
independence is normally fine, but it also means these files have no backup
story beyond the mini's own `.bak-*` files and whatever restic snapshot
happens to be current.

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

## Validator repository identity (`validator_repo_guard.py`, `repo-aliases.json`)

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
scp machine-setup/mini-scripts/validator_repo_guard.py mini:~/.hermes/scripts/validator_repo_guard.py
scp machine-setup/mini-scripts/repo-aliases.json      mini:~/.hermes/config/repo-aliases.json
python3 machine-setup/mini-scripts/tests/test_validator_repo_guard.py            # hermetic
HERMES_REPO_GUARD_LIVE=1 python3 machine-setup/mini-scripts/tests/test_validator_repo_guard.py  # + real gh
```

The PR review/merge closure is the exception to the manual-copy convention:
its canonical sources, deterministic manifest, and deployment reconciler live
under `pr_pipeline/`. Do not compare or copy those files one at a time.

## Files

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
  served rate. Exit codes: `0` healthy or disabled-or-smoke-only, `2`
  degraded (provider genuinely failing on real traffic), `3` not-observed
  (stage never ran / ledger missing or stale), `4` insufficient-data (fewer
  than `--min-attempts` real, non-smoke attempts in the lookback window), and
  `5` fetch-degraded (`fetch_success_rate < 0.50`) — the JSON `status` field
  is authoritative, and the exit code exists for consumers that only check
  process exit status. The JSON also exposes
  `fetch_success_band` as `alarm`, `warn`, `healthy`, or `null`; the warning
  band is `0.50 <= fetch_success_rate < 0.70`. `--quiet-when-healthy`
  suppresses stdout (still exits 0) when status is `healthy` or
  `disabled-or-smoke-only`, except that a `warn` fetch band remains visible
  so the Mini cron/Slack delivery is actionable without changing status or
  exit codes. Every non-success status still prints the full JSON. Cross-run
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
- `pr_staleness_alert.py` — Slack-delivered cron wrapper (mini job
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
  component. The legacy flat entry points and the package namespace are both
  generated Mini artifacts from these sources.
- `reconcile_pr_pipeline.py` — the only deploy/verify path for that closure.
  It installs only manifest paths, records the supplying source commit plus
  every deployed SHA-256 in `.pr_pipeline_deployment.json`, and reports
  missing, extra, or drifted pipeline files. The snapshot is hard-shadowed:
  this deployment surface never enables or invokes a live merge.

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

Fix: `pr_staleness_alert.py` no longer calls that function at all. It calls
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
scp machine-setup/mini-scripts/pr_staleness_alert.py \
    mac-mini-h.tail51ec1b.ts.net:~/.hermes/scripts/pr_staleness_alert.py
mini-run -- 'python3 -m py_compile ~/.hermes/scripts/pr_staleness_alert.py'  # sanity check
```

## PR-pipeline source and deployment integrity

`machine-setup/mini-scripts/pr_pipeline/` is now the only authoritative
source for the Mini PR-review/merge closure. The Mini copies at
`~/.hermes/scripts/` and `~/.hermes/scripts/pr_pipeline/` are generated
artifacts. The manifest includes the legacy flat entry points and every Python
file in the package, so a newly added trust-boundary module cannot silently
remain Mini-only.

The focused verifier/reconciler is deliberately separate from the legacy
patch checker: it stages the manifest sources to the Mini, writes only the
manifest destinations and deployment marker, then checks SHA-256 parity and
the recorded source commit. Supply the exact already-approved source commit;
the tool never derives it from, or mutates, a Mini checkout.

```bash
python3 machine-setup/mini-scripts/reconcile_pr_pipeline.py reconcile \
  --host mini --source-commit <approved-source-commit>

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
for "Claim retry cap" above, or just `pr_staleness_alert.py` for "PR
staleness dedupe" above) — never the directory. The same caution applies to
`~/.hermes/skills/clickup-queue-poller/scripts/` — copy only `claim_next.py`,
never the directory.
