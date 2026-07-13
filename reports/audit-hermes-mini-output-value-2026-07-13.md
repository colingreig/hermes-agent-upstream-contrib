# Hermes Agent Configuration Audit — Mac mini Output Value

**Date:** 2026-07-13

**Mode:** Read-only audit; no live configuration, service, database, or source changes

**Authoritative runtime:** the single Hermes instance on the Mac mini (`ssh mini`)
**Audit lens:** useful output = correct, actionable, concise, reliable, low-noise, and cheap enough to sustain

## Executive conclusion

Hermes has a good reasoning and capability foundation, but its value is currently being suppressed by operational noise and state-integrity failures—not by an underpowered primary model.

The primary interactive route is already strong (`openai-codex` / `gpt-5.5`, medium reasoning), the identity files are compact, and skill discovery is broad without forcing every skill into the permanent tool schema. The highest-return improvements are therefore:

1. stop unchanged incidents from repeatedly posting into Slack;
2. bound cron concurrency and make transcript/system-prompt writes lossless;
3. close the actual compression-chain tip after cron jobs finish, then reconcile leaked active sessions safely;
4. make provider and job health truthful rather than treating fallback or agent completion as objective success; and
5. align durable USER/MEMORY instructions with the current Prep → Executor → Validator contract.

This audit supersedes the live-profile premise in the first July 13 configuration report. The MacBook `~/.hermes` is development/test state; the Mac mini is the only live Hermes.

## Scope and method

Read-only evidence was collected from:

- the Mac mini's running processes, LaunchAgents, `~/.hermes/config.yaml`, `SOUL.md`, `USER.md`, `MEMORY.md`, cron registry, skill roots, logs, and SQLite state store;
- the deployed Hermes checkout at `~/.hermes/hermes-agent`;
- the repository implementation of session persistence, compression, pruning, and cron scheduling; and
- a bounded sample of the live Slack session history.

No credential values were printed or inspected. No live file, job, service, database row, or source file was changed. The only audit writes are this report and the linked ClickUp action records.

## What is already working well

### Strong default reasoning route

- Interactive primary: `openai-codex` / `gpt-5.5`.
- Reasoning effort: medium.
- First fallback: `openai-codex` / `gpt-5.4-mini`, which is a sensible cost/performance step-down when the provider itself is healthy.
- Compression begins at 50% context, targets 20%, and protects the latest 20 messages. That is directionally good for long-lived conversations and cache cost.

The audit found no evidence that downgrading the primary model would improve value. Reliability and routing truthfulness should be fixed before tuning model spend.

### Compact identity and preferences

- `SOUL.md` is about 1.6 KB and establishes a clear general identity plus important 1Password handling rules.
- `USER.md` and `MEMORY.md` encode action orientation, recommendation-first decisions, task capture, and ClickUp conventions.
- Memory is enabled with bounded character budgets rather than an unbounded context dump.

The prompt foundation is useful and relatively lean. One workflow rule is stale, but the overall approach should be preserved.

### Capability at the edges

- The live user skill root contains 107 skills; the deployed repository contains 72 built-in skills.
- Tool search is automatic once the tool count exceeds ten, helping keep the always-present schema narrower.
- The sole active messaging surface is Slack; Telegram is disabled. This matches a focused personal-agent deployment.
- Smart approvals are enabled and cron approval mode is deny, appropriate for unattended work.

## Findings

### F1 — High, confirmed: Slack is an alert stream instead of a conversation surface

**Evidence**

In the latest 24-hour Slack history sample, Hermes appended 57 assistant messages and zero user messages:

- 40 messages were ten copies each of the same four `agent-review` task alerts;
- 16 were the same `ignite-sentinel` 1Password-resolution failure; and
- one reported an off-box backup failure.

The ClickUp alert code explicitly intends a 72-hour re-nudge interval. In the live `~/.hermes/scripts/clickup_review_sla.py`, the no-comment path records `no_park_nudge_ts`, but the later cleanup pass removes records that are not in a review status and have no `decision_thread_ts`. These four no-comment decision parks satisfy that deletion condition. The next hourly run therefore sees no prior notification and sends again.

The sentinel runner has a separate but related design problem: its EXIT trap sends on every non-zero hourly run, with no persistent incident fingerprint, state-transition guard, or recovery message.

**Why this reduces value**

Urgent output stops being salient. Repeated unchanged messages consume attention, bury real conversations, and train the user to ignore Hermes.

**Recommendation**

Use incident lifecycle output:

- immediate message on a new actionable incident;
- no repeat while the fingerprint and severity are unchanged;
- immediate update on material change or recovery; and
- unresolved incidents summarized in a scheduled digest.

Preserve the existing 72-hour task re-nudge contract as an outer backstop. Fix the delete-after-record bug and add persistent deduplication to sentinel/backup alerting.

**Action:** [86e2aa5kg](https://app.clickup.com/t/86e2aa5kg) — `model:sonnet`

### F2 — High, confirmed: compressed cron sessions leak active continuation tips

**Evidence**

The live state store measured:

- 7,213,662,208 bytes;
- 8,604 sessions;
- 355,154 messages; and
- 3,165 sessions with `ended_at IS NULL`.

Cron accounts for 1,796 of the active sessions. Even on July 13, 22 cron sessions were still open at the sample time. The recent open rows were predominantly untitled `gpt-5.4-mini` continuation IDs, while their preceding rows ended with `end_reason='compression'`.

The source trace confirms the lifecycle bug:

- `agent/conversation_compression.py` ends the old session, changes `agent.session_id` to a new continuation ID, and creates the child session;
- `cron/scheduler.py` retains `_cron_session_id`, then its `finally` block titles and ends only that original ID; and
- `hermes_state.py::prune_sessions()` intentionally deletes only rows whose `ended_at IS NOT NULL`.

After any compression, the original ID is already a compression ancestor and the active tip is a different ID. Closing the ancestor again is a no-op; the real tip remains active and permanently ineligible for retention pruning.

**Why this reduces value**

History, search, pruning, titles, and operational diagnostics become less trustworthy. The database grows with sessions that appear resumable but are actually abandoned job continuations.

**Recommendation**

At job completion, close the current `agent.session_id` or resolve the compression-chain tip from the original cron ID. Add a backup-first, dry-run reconciliation command for provably abandoned active sessions. Do not mass-close interactive sessions based only on age.

**Action:** [86e2aa5km](https://app.clickup.com/t/86e2aa5km) — `model:opus`

### F3 — High, confirmed: unbounded cron concurrency is producing lost state writes

**Evidence**

- Live `cron.max_parallel_jobs` is `null`.
- The scheduler implementation resolves null/zero as unbounded, despite the website reference saying the default is four.
- The 24 enabled jobs cluster around five-, fifteen-, thirty-, and sixty-minute boundaries.
- July 13 logs repeatedly record `Session DB append_message failed: database is locked`.
- Two failures of `update_system_prompt` explicitly state that subsequent turns will rebuild the prompt and miss the cached prefix.
- Failed message persistence is logged, but the agent continues. The resulting transcript can omit tool or assistant state while a cron run is still recorded as successful.

**Adversarial limitation**

The 7.21 GB file size alone does not prove that size caused the locks. Multiple writers, long FTS transactions, maintenance, and schedule bursts can all contribute. The confirmed claim is narrower: concurrent live writes are failing, state is lost, and the configured scheduler does not bound parallelism.

**Recommendation**

Start the Mini at an explicit maximum of four concurrent cron jobs, measure lock rate and completion latency, and lower to two if persistence failures continue. Run a real multi-process WAL/FTS load test before changing retry constants. Critical transcript and system-prompt writes should queue durably or fail the job visibly rather than degrading silently.

**Action:** [86e2aa5kt](https://app.clickup.com/t/86e2aa5kt) — `model:opus`

### F4 — High, confirmed: configured provider health and reported job success are not truthful

**Evidence**

- The primary interactive route is configured and operating; the earlier MacBook-based missing-model claim was false for the live agent.
- `clickup-executor` is configured to start on `zai/glm-4.7`, but July 13 runs repeatedly report that the z.ai credential pool has no usable entries and fall back to OpenAI Codex.
- Gemini appears in general and auxiliary fallback chains, but logs repeatedly report its pool entry has no usable secret or that the provider is not configured.
- At 02:18, an OpenAI timeout reached the unusable Gemini route and exhausted the fallback chain; the agent reported that zero work could occur until recovery.
- Cron metadata can still show `last_status='ok'` when the agent returned a coherent failure explanation but did not accomplish the job objective.

**Why this reduces value**

The configuration overstates resilience, wastes time probing known-dead routes, and makes the board look healthy while work is not happening.

**Recommendation**

Add one shared, read-only effective-routing health resolver for doctor, config check, setup, gateway startup, and cron creation. It should show route order, provider/model, credential source without values, cooldown, and a synthetic health result. Keep one tested cross-provider fallback. Separate “agent returned normally” from “job objective succeeded.”

**Corrected existing action:** [86e2a8mk4](https://app.clickup.com/t/86e2a8mk4) — `model:sonnet`

### F5 — High, confirmed: durable personal instructions contradict the board safety contract

**Evidence**

The live Mini's `USER.md` says new Slack work should be captured as a ClickUp task tagged `agent-ready`. `MEMORY.md` repeats that tag as the execution-queue trigger.

The governing Thermal/Ignite contract says the opposite: new tasks default to no `agent-ready`; the tag is valid only after a canonical execution brief says YES, product decisions are recorded, predecessors are validator-complete, and exactly one model-floor tag exists. The live working directory has no root `AGENTS.md` that would reliably override the stale personal instruction for general Slack requests.

**Why this reduces value**

Hermes can correctly follow its personal memory and still bypass Prep, enqueue unresolved work, or violate the executor/validator handshake.

**Recommendation**

Preserve “capture work instead of live-coding it,” but encode the complete Prep → Executor → Validator contract in USER/MEMORY. Add a read-only prompt-contract diagnostic for contradictory durable rules. Keep the resulting stable prompt concise and byte-stable.

**Action:** [86e2aa5m3](https://app.clickup.com/t/86e2aa5m3) — `model:sonnet`

### F6 — Medium, confirmed incident; existing action path: 1Password is a shared operational bottleneck

**Evidence**

The Mini repeatedly failed sentinel secret resolution, the off-box backup reported a vault rate limit, and agent/tool logs show repeated 1Password failures. This affected monitoring, backup, ClickUp access, and agent execution within the same day.

`SOUL.md` says to stop immediately on a 429 and never retry. Individual processes may obey that rule, but the machine-level fleet still retries on independent schedules, creating a distributed retry loop and repeated user-facing alerts.

**Recommendation**

Treat the 1Password service account as a shared dependency with a single machine-level circuit breaker, cached non-secret health state, randomized recovery probe, and one incident lifecycle. Do not create a second competing remediation task; attach this evidence to the existing degraded-secrets/1Password work.

## Adversarial review

Each major claim was challenged before inclusion.

| Candidate claim | Strongest counterargument | Verdict |
|---|---|---|
| The primary model is the quality problem | The live agent already uses GPT-5.5 at medium reasoning; failures occur before or around inference | **Refuted** |
| The Mac mini has no model configured | That evidence came from the MacBook test profile | **Refuted; prior task corrected** |
| Slack reminders are merely frequent by design | Code specifies a 72-hour re-nudge but deletes its own timestamp each run | **Confirmed bug** |
| A 7.21 GB DB necessarily causes locks | Size is correlated, not sufficient causal proof | **Downgraded; no causal claim** |
| Active sessions are legitimate resumable history | Recent cron rows form compression chains whose original roots are closed but final tips are not | **Confirmed lifecycle bug** |
| Fallback warnings are harmless because OpenAI usually works | The chain reached total exhaustion, and unusable routes remain advertised | **Confirmed reliability gap** |
| `agent-ready` is just a harmless preference | It directly contradicts the current non-negotiable board contract | **Confirmed instruction drift** |
| More alerts improve safety | Forty unchanged task alerts and sixteen identical failures produced no new decision context | **Refuted** |

## Recommended sequence

### First 24 hours

1. Fix the ClickUp no-comment alert-state deletion and suppress unchanged sentinel failures.
2. Set `cron.max_parallel_jobs: 4` as a measured starting cap.
3. Remove or repair unusable z.ai/Gemini routes only after a synthetic health check; ensure the remaining fallback is cross-provider.
4. Update USER/MEMORY so new task capture does not add `agent-ready` before Prep.

These are recommendations only; the audit did not make the live changes.

### Next implementation cycle

1. Fix cron finalization to end the active compression tip.
2. Add safe stale-session reconciliation and run it dry-first against a backup.
3. Load-test SQLite WAL plus FTS under the chosen cron cap.
4. Make persistence and objective failures visible in cron status.
5. Add effective-route health diagnostics to the shared configuration path.

### Then tune quality and cost

Only after reliability is stable:

- measure task success, correction rate, latency, and cost by job/model;
- keep GPT-5.5 for ambiguous interactive judgment;
- use GPT-5.4-mini for bounded validation/execution only where outcome evidence is strong; and
- remove scheduled jobs whose outputs do not change a decision or state.

## Decision requested

There are three defensible notification policies:

1. **State changes + twice-daily digest (recommended):** immediate first occurrence and recovery; unchanged open incidents appear in a morning/evening digest. Best balance for one Hermes running many automations.
2. **State changes only:** quietest, but unresolved incidents can disappear from attention.
3. **Per-incident reminders:** keep individual reminders, but never more often than the existing 72-hour interval. Strongest pressure, highest residual noise.

Recommendation: choose option 1. It preserves urgent visibility without letting infrastructure output take over the conversation surface.

## Action ledger

| Priority | Action | Model floor | Status |
|---|---|---|---|
| High | [Stop repeated Slack alerts](https://app.clickup.com/t/86e2aa5kg) | `model:sonnet` | `to do`; no `agent-ready` |
| High | [Close compressed cron tips and reconcile stale sessions](https://app.clickup.com/t/86e2aa5km) | `model:opus` | `to do`; no `agent-ready` |
| High | [Bound cron concurrency and make state writes lossless](https://app.clickup.com/t/86e2aa5kt) | `model:opus` | `to do`; no `agent-ready` |
| High | [Add truthful provider and cron outcome diagnostics](https://app.clickup.com/t/86e2a8mk4) | `model:sonnet` | corrected existing task; `to do` |
| High | [Align USER/MEMORY with the workflow contract](https://app.clickup.com/t/86e2aa5m3) | `model:sonnet` | `to do`; no `agent-ready` |

The earlier MacBook isolation task remains valid as development-environment hygiene and is explicitly not a second Hermes deployment: [86e2a8mbh](https://app.clickup.com/t/86e2a8mbh).
