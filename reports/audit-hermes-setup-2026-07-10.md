# Hermes Setup Audit — Security, Efficiency, Effectiveness (+ Strategy)

**Date:** 2026-07-10
**Auditor:** Fable-orchestrated `ignite-audit` (8 dimension agents, 5 verification/refute passes, light web research)
**Scope:** Hermes fork (`hermes-agent/mbabane`), live runtime on the Mac mini (read-only over `ssh mini`), local `~/.hermes`, and `~/dev/ignite-workbench` for the division-of-labor map. Read-only on all systems; this report is the only write.

---

## TL;DR

- **Headline risk is a security *chain*, not a single bug:** the gateway runs with `HERMES_YOLO_MODE=1` (all dangerous-command approvals auto-approved) **+** a local unsandboxed terminal backend **+** ~143 non-Hermes business secrets (Cloudflare, Vercel, Supabase service-role, GitHub PATs, raw DB connection strings, six client WordPress logins) sitting in the gateway's process env behind a *name-only* deny-list. A single prompt injection during any normal session can run `env` and exfiltrate client secrets **with no approval prompt**. All three links **CONFIRMED** against live config.
- **The mini is a single point of failure with no backup:** 36 GB of `~/.hermes` state (config, ledgers, unpushed prod commits, 176 untracked ops scripts) has **zero off-box copy** — Time Machine off, no sync. **Critical, CONFIRMED.**
- **A provider is silently down:** both Gemini keys are genuinely invalid (wrong credential stored in 1Password — not the known quote-wrap false alarm), failing 11 of 14 auxiliary tasks for 2+ days, and `degraded-secrets-monitor` is **blind to it** (it never reads `auth.json`/credential-pool status). **Critical, CONFIRMED.**
- **Good news — the six scary upstream CVEs do NOT apply:** every advisory the web flagged (RCE via context/memory scan, sandbox escape, Discord/webhook/api_server auth bypass) is either already-mitigated in the fork (fix commits are ancestors of the mini's HEAD) or unreachable (Discord/webhook not bridged; api_server loopback + strong bearer key). This is the single most reassuring finding.
- **Effectiveness gap worth Colin's attention:** the validator (`ignite-validate`) produced a **false PASS** by validating a PR in the *wrong repository* — a systemic repo-routing/validation hole, not a one-off.

**Findings:** 4 Critical, 5 High, 8 Medium, 4 Low (+ well-scoped Info/dead-ends). **Proposed:** 5 ClickUp epics (structure below — not yet created, per Colin's instruction to approve first).

---

## 1. Findings (ranked)

Status key: **CONFIRMED** = survived an adversarial refute pass; **UNVERIFIED** = single-pass (Medium/Low, per audit policy).

### CRITICAL

**C1 — Full business-secret bundle is exfiltratable through the `terminal` tool.** *CONFIRMED.*
`~/.hermes/scripts/gateway_secrets_wrap.sh` does `set -a; . "$resolved_env"` and exports ~143 secrets from 1Password's "Dev Toolbox" vault into the gateway process env, then `exec`s the gateway. The terminal tool builds its child env as `dict(os.environ | env)` and strips **only** a name-based `_HERMES_PROVIDER_ENV_BLOCKLIST` (`tools/environments/local.py::_make_run_env`) — deny-list-over-full-inheritance. None of the business secrets match that list, so `terminal("env")` prints them in cleartext: `CLOUDFLARE_API_TOKEN`, `VERCEL_TOKEN`, `SUPABASE_STAGING_SERVICE_ROLE_KEY`/`_DB_PASSWORD`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `D365GROUP_DATABASE_URL` (raw Postgres DSN), six `WP_<site>` client logins, Twilio/Resend/Postmark/Meta/Microsoft-Ads, etc. `execute_code` uses a stronger substring scrubber but ~8 vars (the `WP_*` and `D365*` DSNs) have no secret-substring in their *names* and leak there too.
**Action:** Stop injecting non-Hermes business secrets into the gateway's shared process env — resolve them per-task from 1Password at call time — **or** switch the terminal backend to the same allowlist+substring scrub `execute_code` uses. *(model:opus)*

**C2 — Gateway runs `HERMES_YOLO_MODE=1` process-wide; dangerous-command approvals are silently auto-approved.** *CONFIRMED.*
`~/.hermes/.env` sets `HERMES_YOLO_MODE=1`; Hermes' own entrypoint reloads `.env` into `os.environ` (override) before `tools/approval.py` imports, so `_YOLO_MODE_FROZEN` freezes `True`. In `approval.py`, the YOLO branch returns `{"approved": True}` **before tirith is ever consulted** — so the smart-approval gate (`approvals.mode: smart`, which *is* configured) and the tirith risk-assessor are both bypassed. Only the hardline floor (`rm -rf /`, `mkfs`, fork bomb, shutdown) and a sudo-stdin guard survive.
**Reachability:** No external adversary can walk in (Telegram disabled; Slack locked to one user ID + one channel; api_server loopback-only). The real trigger is **prompt injection** via content the agent ingests during Colin's own sessions (`web_extract` output, MCP tool results, scanned files) — which then chains into C1.
Note: `ps eww` does **not** show this var (macOS shows only the exec-time env snapshot, not Python's `os.environ` mutation) — verify via the import graph, not `ps`.
**Action:** Remove `HERMES_YOLO_MODE` from the gateway's persistent env (reserve it for scoped one-shot CLI calls); let `approvals: smart` + tirith actually gate messaging-triggered commands. *(model:sonnet)*

> **C1 + C2 + C3 together are the report's headline.** Individually each is a "known posture" trade-off; chained, they turn any prompt injection into no-prompt client-secret exfiltration. Fix C1 and C2 first — they collapse the blast radius even if C3's local backend stays.

**C3 — No off-box backup for the entire mini state.** *CONFIRMED.*
`tmutil status` → not running; `tmutil destinationinfo` → no destinations; no sync LaunchAgent. `~/.hermes` is 36 GB on one machine and includes the only copy of: `config.yaml`, `cron/jobs.json`, the ledgers, 176 untracked ops scripts, and unpushed prod commits (see H3/H4). A disk failure erases the live production system with no restore path.
**Action:** Stand up nightly off-box sync (restic/rsync to another host or Time Machine), prioritizing `config.yaml`, `cron/jobs.json`, `~/.hermes/scripts`, and the hermes-agent checkout's unpushed commits. *(model:sonnet)*

**C4 — Both Gemini keys are genuinely invalid; a provider has been silently degraded for 2+ days, and the monitor can't see it.** *CONFIRMED (ruled out the known quote-wrap false alarm).*
`auth.json` credential-pool shows both `gemini` entries `last_status: exhausted`, `HTTP 400 … API key not valid`; `gateway.error.log` has this error ~71 times since ≥2026-07-03. Gemini is primary for 11 of 14 auxiliary tasks. The refute pass confirmed the credential path is clean (bash `source` strips `op_sdk_resolve.py`'s quote-wrapping) and the process isn't stale — the secret stored under the 1Password `Gemini API Credentials` item simply is **not** a valid Google `AIzaSy…` API key (wrong credential/type stored). Meanwhile `degraded_secrets_monitor.py` only watches for 1P relaunch loops and unresolved MCP placeholders — it **never reads `auth.json`/credential-pool**, so a key stuck at 400/exhausted forever never trips an alert.
**Action:** Store the correct Gemini API key in the 1Password item, and extend `degraded_secrets_monitor.py` to alert on any credential-pool `last_status: exhausted/invalid`. *(model:sonnet)*

### HIGH

**H1 — `auxiliary.vision` and `auxiliary.curator` ship with no fallback and are failing live.** *UNVERIFIED (corroborated by C4 logs).* Vision analysis has returned hard tool-errors for 2+ days with zero graceful degradation — exactly the SPOF scenario the fallback work (#15–#19) was meant to close, but these two tasks were deliberately left without a fallback rung. **Action:** wire a vision-capable fallback (Anthropic image path) or at least make vision failures loud (Slack alert). *(model:sonnet)*

**H2 — Uncommitted `onepassword-sdk==0.4.0` pin on the live checkout has never been committed.** *UNVERIFIED (direct git evidence).* The live running checkout (`~/.hermes/hermes-agent`, `prod-live-patches`) has a working-tree edit pinning `onepassword-sdk` in `pyproject.toml`/`uv.lock`, added *because* a prior `uv sync --locked` dropped it and boot-crash-looped gateway + dashboard on 2026-07-08. It's still only a working-tree change — any future `uv sync --locked` from git HEAD reproduces the exact incident. **Action:** commit `pyproject.toml` + `uv.lock` and push to `fork/prod-live-patches` now. *(model:haiku)*

**H3 — Live prod checkout has 12 unpushed commits + uncommitted core-file edits, single-machine.** *UNVERIFIED.* `cron/scheduler.py` (a ~59-line shutdown-race + partial-success fix), `hermes_cli/config.py`, plus 12 `.bak-*` source files exist only on the mini and only in the working tree; branch is 12 commits ahead of `fork/prod-live-patches`. Combined with C3 (no backup), a disk failure silently erases live prod fixes. **Action:** commit + push all pending work on the live checkout. *(model:sonnet — code review before push)*

**H4 — 23 GB of orphaned worktrees; the sweep is a safety backstop, not a GC.** *UNVERIFIED.* `~/.hermes/worktrees` = 40 dirs / 23 GB, ~25 of them stale pre-migration full clones unregistered in any bare mirror; `worktree-backstop-sweep` logs `removed=0` every run (everything is SKIP_DIRTY / SKIP_AHEAD). Disk has only ~24 GB free. One worktree (`ignite-86e261t26`) reports **13,300 commits ahead** (a broken ref) and is permanently skipped, so no human ever sees it. **Action:** add a real land-or-retire triage step (or extend `closeout_actor`) and reclaim the pre-migration orphans. *(model:sonnet)*

**H5 — Validator produced a false PASS on a PR in the wrong repository.** *UNVERIFIED (ClickUp evidence).* Task `86e251kqb` (Trimble/Viewpoint vendor profiles, target `fieldservicesoftware.io`) was delivered to `jdmbuysell-v4` and a prior `ignite-validate` pass marked it PASS by validating evidence from the wrong project; only a later re-validation caught it (`class=process,not-live`). This is a systemic executor/validator repo-routing hole. **Action:** add a repo-identity assertion (PR repo must equal the task's known target) to `ignite-validate`/`hermes-pr-validate`, and re-audit recently-PASSed tasks for the same class. *(model:sonnet)*

### MEDIUM

**M1 — All 9 fallback-equipped aux tasks fall back to the *same* `zai/glm-4.7` credential** — a new single point of failure that is *de facto primary* right now because Gemini (C4) is down. z.ai itself is throwing 401s. **Action:** add a third, provider-diverse rung (Anthropic / openai-codex-mini for text tasks). *(model:sonnet)*

**M2 — z.ai 401 is likely a config-precedence bug:** `.env` hardcodes a plaintext `ZAI_API_KEY` and loads with `override=True` *after* the 1Password value is already in the env — so a stale `.env` copy **wins** over the rotated 1P secret. **Action:** remove the hardcoded `.env` copy so 1Password is authoritative. *(model:sonnet)*

**M3 — `~/.hermes/scripts` (176 operational scripts) is outside any git repo,** versioned only by manual `.bak-<date>` siblings. **Action:** make it its own small git repo (or fold into hermes-agent) and push it. *(model:haiku)*

**M4 — Content-lane backlog is piling up faster than it drains.** 55 unclaimed `agent-ready` to-dos (42 `lane:content` vs 6 `lane:code`), while 7-day completions skew the opposite way (65 code vs 9 content) — the dev-executor crons may only process `lane:code`, leaving content with no dedicated executor. **Action:** confirm content lane has an executor; if not, add one (see roadmap §4). *(model:sonnet)*

**M5 — In-review SLA breaches:** tasks sitting 108 h and 147 h in review despite an hourly `hermes-pr-validate`; several `fieldservicesoftware.io` content tasks at 20–40 h. **Action:** diagnose why the validator isn't clearing the `Agents`-space queue (repo access? missing CI signal? classification gap). *(model:sonnet)*

**M6 — Structural blocks are retried as code defects.** Task `86e22876h` (LCP < 2.5 s) looped ≥5 executor↔validator rounds because the repo has no PR-triggered Lighthouse/CI — neither side can ever produce the measurement. **Action:** detect "no measurement infrastructure exists" as a distinct failure class routing to needs-human, not re-queue. *(model:sonnet)*

**M7 — Three full-agent crons load the 287-skill catalog unscoped:** `w` (every 5 min, prompt literally `echo hi` → ~288 full-catalog loads/day), `alpha` (hourly), `clickup-reconciler` (daily). **Action:** set `skill_scope` on all three, or disable `w` if it's a leftover test. *(model:haiku)*

**M8 — `~/.hermes/secrets/clickup_api_token.txt` is world-readable (644)** on the mini, unlike every sibling secret (600). **Action:** `chmod 600` and audit the directory for siblings. *(model:haiku)*

### LOW

**L1 —** `hermes mcp serve` (19) + `basic-memory mcp` (22) long-lived processes running simultaneously (oldest ~18 h); possible session-child leak with no reaper. *(model:sonnet)*
**L2 —** Content-writer fallback `openai/gpt-5` hard-times-out (rc=-9) recurringly; reorder/shorten its per-model timeout so it fails fast down the cascade. *(model:sonnet)*
**L3 —** `~/.hermes/state.db` (5 GB) + `db-backups/*.json` world-readable (644) on the mini. *(model:haiku)*
**L4 —** Local MacBook `~/.hermes/google_token.json` (plaintext OAuth `client_secret`/`refresh_token`) world-readable (644). *(model:haiku)*

---

## 2. Dead ends (what was checked and found clean — half the value of an audit)

- **All six upstream CVEs are non-issues in this fork.** Verified against the actual code + live config, not the advisories:
  - CVE-2026-9366 (`_scan_context_content`) and CVE-2026-10223 (`_scan_memory_content`) — these functions are the **mitigations**, not the vulnerabilities; they replace flagged content with `[BLOCKED]` placeholders and have no code-execution path.
  - CVE-2026-9368 (`execute_code` env handler) — closed by `_is_hermes_provider_credential` / blocklist, fail-closed; fix `3ab7e2aa9` present on the mini.
  - CVE-2026-14627 (Discord allowlist) — **Discord isn't bridged** in live config (not in `platforms:`); code also already fixed.
  - GHSA webhook/api_server auth-bypass — webhook not configured; api_server is loopback-only with a 48-char bearer key and `_check_auth` on every route.
  - All named security-fix commits are confirmed ancestors of the mini's HEAD. Colin's fork is *ahead* of the vulnerable versions.
- **1Password integration is well-designed** (official SDK, 600-perm runtime token, reference-only `op-secrets.env`) — the leak is downstream (C1), not the vault flow.
- **GitHub credentials are sound** — per-request ~1 h GitHub App installation tokens via a Hermes-only credential helper, never persisted; App private key 600; no outbound-SSH capability configured.
- **Inbound authorization fails closed** — per-platform allowlists, single Slack user + channel, api_server keyed. (Caveat: within the authorized set all callers are equally trusted, so an authorized session is as powerful as Colin — which is *why* C1/C2 matter.)
- **Core services healthy** — gateway/dashboard/codex-proxy running, no crash loops, scheduler tick alive, no hung TCC-stuck sandbox processes, spend low (~$5.82 over 15 days), the one historical cascade-exhaustion event is already Slack-monitored.

---

## 3. Where Hermes is heading — strategy & division of labor

Colin runs **two** agent systems. The single most valuable thing this section can do is draw a clean line between them so work stops landing in the wrong place.

### 3a. The charter (recommended)

| | **ignite-workbench** — *"the factory"* | **Hermes** — *"the workshop crew + night watch"* |
|---|---|---|
| **What it is** | Production Next.js/Vercel agency-ops platform (client-facing). Deterministic Inngest pipelines grounded in real data. | Autonomous, messaging-bridged agent fleet on the mini working ClickUp boards. |
| **Owns** | SEO audits, PPC audits, monthly client reports, decks, ad-copy loop, AOE link-building, BigQuery ETL, Tarvec client portal, content publishing. | Code changes on repos, backlog execution/QA/validation, ingest/triage, monitoring, ad-hoc research + **content drafts**, personal-assistant messaging. |
| **Data grounding** | Google Ads / GSC / GA4 / Ahrefs / BigQuery / vendor APIs. | The repos + ClickUp + messaging. |
| **Human gate** | Client-facing artifacts approved before send. | PRs reviewed; `ignite-validate` QA gate. |

**Rule of thumb for "who does what":**
- Needs client-data grounding + auditability + a client-approved artifact → **workbench**.
- Autonomous labor / maintenance / triage that produces an internal artifact or a PR → **Hermes**.
- **Neither should own the other's crown jewel.** Hermes should *not* run client-facing SEO/PPC pipelines (they belong in workbench's grounded, auditable Inngest jobs); workbench should *not* try to be the autonomous code-maintenance fleet.

**Collision zones to manage (both systems touch these):** ClickUp writes (keep board namespaces distinct), Fireflies (watch shared rate limits), autonomous email drafting (shared review convention), and separate Anthropic keys (fine — but consolidate cost tracking if you want one spend view).

### 3b. Growth roadmap — prioritized *content + SEO first*, per Colin

1. **Content writing (do first).** Adopt the 2026 consensus editorial pipeline: **Research → Writer → SEO-review → QA/Editorial → Distribution**, where the QA agent scores brand-voice + flags factual/hallucination risk so the human **reviews only the flagged sections**, tracked by a **"Human Edit Delta"** metric (how much the human still changes post-QA) as the quality signal. *Placement:* the publishable pipeline lives in **workbench** content-ops (it already has publish adapters); **Hermes supplies draft labor**. But first fix **M4** — Hermes' content lane has a backlog with no executor, so more content demand today just deepens the queue.
2. **Proactive SEO (do first, with content).** Shift from "flag the drop" to **"propose the diff"**: weekly GSC/Ahrefs snapshot → content-decay + claims audit → auto-generate a one-click-apply refreshed draft. *Gap neither system covers today:* a **between-report GSC regression watcher** (workbench only checks at monthly cadence). *Placement:* workbench (it owns the SEO audit engine + data); Hermes can run the recurring trigger.
3. **Proactive PPC auditing.** The current PPC audit is on-demand only. Add **scheduled Google Ads MCP audits** (a standard 2026 pattern — QS monitoring, budget pacing, conversion-tracking validation as distinct weekly workflows grounded in the real Ads API, not scraped dashboards). *Gap:* no anomaly detection between audits. *Placement:* workbench (owns the PPC engine + BigQuery ads data).
4. **Coding throughput.** Hermes already *is* the headless-Claude-Code fleet pattern (`ignite-execute`). Level-ups are the effectiveness fixes above: repo-identity guard (H5), a content-lane executor (M4), a structural-block failure class (M6), SLA clearing (M5), plus wall-clock timeout + concurrency guards per 2026 best practice.

### 3c. Harness best practices worth borrowing (from web research)

- **Per-task scoped, short-lived credentials** instead of a whole bundle in the daemon env (directly addresses C1).
- **Hard per-cron token/cost ceilings + wall-clock timeouts** at the gateway layer, not just provider dashboards (autonomous agents burn ~50× single-turn tokens; real runaway-bill incidents in 2026).
- **Structured observability** (self-hosted Langfuse/Phoenix; secrets never leave the box) — you can't answer "why did it do that" in an incident review without it. Also a *security* control: 2026 consensus is behavioral/output monitoring beats input filtering, because adaptive prompt injection reliably beats filters.
- **Lethal-trifecta discipline:** any surface that ingests untrusted content *and* can call ClickUp/messaging *and* touches secrets should have its tool scope explicitly narrowed — the durable defense is removing a leg, not a system-prompt guard.

---

## 4. Proposed ClickUp epics (NOT yet created — awaiting Colin's approval)

Grouped so each epic is a coherent workstream. Model tags are floors.

1. **EPIC — Hermes Security Hardening.** C1 (env-secret isolation / per-task secret resolution), C2 (remove YOLO from gateway env), C3-adjacent backend-sandbox decision. `model:opus`/`model:fable` (security-sensitive, needs Colin's posture call). *Highest priority.*
2. **EPIC — Hermes Resilience & Backup.** C3 (off-box backup), H2 (`onepassword-sdk` pin), H3 (commit/push live patches), H4 (worktree GC + broken-ref), M3 (scripts into git). `model:sonnet`.
3. **EPIC — Provider & Routing Health.** C4 (Gemini key + monitor gap), H1 (vision/curator fallback), M1 (aux fallback diversity), M2 (z.ai `.env` precedence), M7 (cron skill-scope), L1/L2. `model:sonnet`.
4. **EPIC — Fleet Effectiveness.** H5 (validator repo-identity guard), M4 (content-lane executor), M5 (SLA clearing), M6 (structural-block failure class). `model:sonnet`.
5. **EPIC — Division of Labor & Growth Roadmap.** The workbench↔Hermes charter (§3a) as a written doc, plus the content QA-gate pipeline, proactive SEO decay watcher, and proactive PPC audit cron (§3b). `model:fable` (architectural/charter). *Some items land in workbench, not Hermes.*

Quick hygiene tasks that can ship immediately regardless of epic approval: **M8** (`chmod 600` clickup token), **L3/L4** (`chmod 600` state.db + google_token.json). `model:haiku`.

---

## 5. Durable cross-project learnings (worth remembering beyond this report)

1. **Two hermes-agent checkouts exist on the mini; only `~/.hermes/hermes-agent` is authoritative** (the gateway runs from `~/.hermes/hermes-agent/venv/bin/hermes`, HEAD `2c080204f9`). `~/dev/hermes-agent` is a stale second checkout ~24 commits behind — a debugging trap that made one agent briefly conclude a deployed fix was undeployed. Delete or clearly mark it non-authoritative.
2. **`ps eww` on macOS does not show a process's runtime `os.environ` mutations** — Hermes reloads `.env` into `os.environ` at Python import, so verify daemon env via the import graph, not `ps`.
3. **`degraded-secrets-monitor` is blind to credential-pool status** (only watches 1P relaunch loops + MCP placeholders) — a known monitoring gap until C4's fix lands.
4. **`.env` loads with `override=True` *after* 1Password resolution**, so a stale hardcoded plaintext value in `.env` silently wins over the rotated 1P secret (root of M2) — a config-precedence trap to check first when a "rotated" key still fails.

---

## Method

| Dimension | Model | Notes |
|---|---|---|
| Security | sonnet | 3 Criticals raised |
| Model routing / spend | sonnet | Gemini Critical, aux SPOF |
| Reliability / ops | sonnet | Backup Critical, worktree bloat |
| Effectiveness | sonnet | Validator false-PASS, backlog |
| Inventory sweep | haiku | Layout, cron fleet, versions |
| ignite-workbench map | sonnet | Division-of-labor input |
| Web research | sonnet | Upstream CVEs, best practices |
| CVE reachability verify | sonnet | All 6 CVEs cleared |
| Adversarial refute ×4 | sonnet | YOLO, local backend, env leak, Gemini/quote-wrap |

Rounds: 1 fan-out (8) + 1 verification round (5). All Critical/High findings that carry **CONFIRMED** survived an adversarial refute pass; Medium/Low ship **UNVERIFIED** per audit policy. Coverage bounded to the `--depth standard` budget (no extension rounds needed — verification round was the extension).
