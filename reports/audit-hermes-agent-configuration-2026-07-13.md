# Hermes Agent Configuration Audit — Output Quality and User Value

**Date:** 2026-07-13  
**Scope:** The default local Hermes profile (`~/.hermes`), its persisted configuration and prompt/memory/skill inputs, plus the repository paths that load, validate, and act on that configuration.  
**Boundary:** Read-only on the codebase and live profile. No configuration, cron job, prompt, memory, or source file was changed by the audit.

## TL;DR

- Hermes' built-in prompt is already strong; the largest quality problem is that this instance contributes almost no durable operator context on top of it.
- Two High risks are **CONFIRMED**: tests have polluted the real cron store with enabled fixture jobs, and the profile has no persistent default model, which has already caused scheduled work to fail.
- The local identity is the stock template, `USER.md`/`MEMORY.md` are absent, and there are no local domain-specific skills or plugins, so output cannot reliably reflect Colin's priorities or repeatable Ignite workflows.
- The first-contact profile builder is latched as “offered” even though it produced no user profile, and there is no command to restart that guided flow.
- **2 ClickUp action tasks** are created from the confirmed findings; four Medium recommendations remain explicitly **UNVERIFIED** under the audit policy.

## Findings

Status key: **CONFIRMED** survived an adversarial refutation pass. **UNVERIFIED** is a single-pass Medium finding, as required by the standard-depth audit policy.

### High — CONFIRMED

#### H1 — The real cron store is polluted by test fixtures, including 10 enabled full-agent jobs

The default profile's `~/.hermes/cron/jobs.json` contains 24 fixture-shaped jobs. Ten are enabled: five `w` jobs with prompt `echo hi` every five minutes, one `alpha` job with `say hello` hourly, three `claim job` entries with prompt `x`, and one stale one-shot. None has a model or skill restriction. The interval jobs alone represent **1,464 scheduled full-agent starts per day** if the scheduler can resolve a model.

The payloads match repository tests exactly:

- `tests/cron/test_jobs_changed_notify.py:71-83` creates `w` / `echo hi` / every five minutes.
- `tests/hermes_cli/test_console_engine.py:604-607` creates `alpha` / `say hello` / hourly.
- `tests/cron/test_claim_job_for_fire.py:13-18` says the test home is isolated and claims `cron.jobs` does not cache the home at import.
- In reality, `cron/jobs.py:64-72` captures `HERMES_DIR`, `CRON_DIR`, `JOBS_FILE`, output, and ticker paths at module import.
- `tests/conftest.py:330-363` changes `HERMES_HOME` only when the autouse fixture runs, after pytest collection can already have imported `cron.jobs`.

The live job creation timestamps span July 6–13 and track repeated test executions. This is not just stale user experimentation: the exact fixture payloads recur across dates, and the newest `claim job`/one-shot group was written within one second.

**Refutation result:** Survived. The strongest alternative explanation—intentional user-created test jobs—does not explain the exact multi-file fixture payloads, repeated creation cadence, or the import-time path/fixture-order mismatch. Upstream PR [#63519](https://github.com/NousResearch/hermes-agent/pull/63519) independently reproduces the same root cause, is focused, clean, and fully green. Draft PR #60882 also touches dynamic cron paths but contains extensive unrelated changes and is not the preferred delivery vehicle.

**Suggested action:** Merge/rebase the focused isolation fix from PR #63519, add a regression that direct multi-file `pytest` cannot touch the real profile, and—after Colin approves the destructive cleanup—remove the known fixture jobs from the live store. Keep production-path dynamic resolution as a focused follow-up only if it is still needed after the test fix. **Minimum model: sonnet.**

### High — CONFIRMED

#### H2 — The profile has no persistent default model, so unattended work is not runnable

The raw `~/.hermes/config.yaml` is only 80 bytes and contains two onboarding latches. It has no `model.default` or `model.provider`. `~/.hermes/auth.json` contains no persisted provider entries, and there is no `~/.hermes/.env`. The active shell exposes some provider-specific keys, but no `HERMES_MODEL`; every enabled cron artifact also has an empty per-job model.

This already manifests at runtime. `~/.hermes/logs/agent.log` records three separate `Check server status` jobs failing on July 12 at `22:57`, `23:00`, and `23:04` with:

> config.yaml model.default missing or empty

That message comes from the scheduler's fail-fast guard in `cron/scheduler.py:2833-2856`. Provider auto-detection cannot rescue cron because cron requires a concrete model before provider resolution.

**Refutation result:** Survived. A temporary per-run or per-job model can make an individual invocation work—as later Codex refresh tests demonstrate—but it does not make the profile durable or unattended-ready. Current persisted state still lacks every model source the scheduler accepts.

**Suggested action:** Persist an explicit primary `model.provider` + `model.default`, add a tested fallback appropriate to the account, and add a readiness diagnostic that fails clearly when enabled agent crons have no resolvable model. `hermes config check` should not present this profile as configuration-complete. **Minimum model: sonnet.**

### Medium — UNVERIFIED

#### M1 — The agent has no operator-specific identity or user context

`~/.hermes/SOUL.md` is byte-for-byte the 513-byte stock Hermes identity: helpful, knowledgeable, direct, and generic. `~/.hermes/memories/` contains neither `USER.md` nor `MEMORY.md`. The feature is enabled by default (`memory.memory_enabled: true`, `memory.user_profile_enabled: true` in `hermes_cli/config.py:2231-2248`), but it has no content to inject.

The runtime is therefore forced to rediscover Colin's role, businesses, decision style, delivery bar, and current priorities in every new session. The system prompt can make the model diligent, but it cannot make an unconfigured instance know what “valuable” means to this operator.

**Suggested action:** Replace the stock SOUL with a short, stable communication contract; create a compact `USER.md` containing confirmed operator facts and priorities; keep repo rules in `AGENTS.md`; and enable `memory.write_approval: true` while the profile is being established so incorrect assumptions do not silently become durable. Do not duplicate project instructions across all three layers. **Minimum model: fable** for the initial content/judgment, **haiku** for mechanical persistence after approval.

#### M2 — Profile onboarding records “offered,” not “completed,” and the value path disappears after one message

The raw config says `onboarding.seen.profile_build_offered: true`, but no `USER.md` exists. In `gateway/run.py:10954-10968`, Hermes appends the offer and immediately calls `mark_seen(...)` before the user accepts or any profile fact is saved. `agent/onboarding.py:157-184` describes a good consent-gated flow, but the latch measures exposure rather than outcome. The slash-command registry contains `/profile`, `/personality`, and `/memory`, but no “build/update my user profile” command that replays the guided flow.

This installation is the concrete failure shape: the product believes onboarding happened; the personalization artifact is absent; the user will not be offered the flow again.

**Suggested action:** Split `profile_build_offered` from `profile_build_completed`, mark completion only after at least one confirmed user fact is persisted (or an explicit permanent decline), and add a discoverable `/profile build` or equivalent desktop action. **Minimum model: sonnet.**

#### M3 — No local domain skill turns recurring Ignite work into repeatable high-value output

The local skills tree contains bundled categories and bundled skills only; every installed skill is represented in `.bundled_manifest`. There is no local plugin directory and no custom skill encoding Ignite-specific deliverables, recommendation formats, ClickUp handoffs, client-report conventions, or meeting follow-up structure.

Hermes can still perform these tasks ad hoc, but the output contract must be restated each time and quality depends on the wording of the latest prompt. This is exactly the kind of repeated behavior the project's Footprint Ladder assigns to a **CLI command + skill**, not a new core tool.

**Suggested action:** Start with two or three narrow, consumed skills rather than a generic “Ignite” mega-skill—for example: (1) meeting transcript → decisions/owners/ClickUp-ready actions, (2) client research → recommendation-first brief with sources and confidence, and (3) daily board digest → exceptions, blockers, and decisions needed. Each should define an output schema and a small evaluation checklist. **Minimum model: fable** for workflow design; **sonnet** for implementation.

#### M4 — Configuration diagnostics do not answer the most important question: “Can this profile produce an answer?”

`hermes config check` reports schema version plus required/optional environment-variable presence (`hermes_cli/config.py:8240-8280`). It does not call `validate_config_structure()` and does not resolve the active model/provider. `validate_config_structure()` itself focuses on malformed custom providers and fallbacks (`hermes_cli/config.py:5184-5307`). `hermes doctor` validates model/provider shapes only when those fields are present (`hermes_cli/doctor.py:737-911`); the empty-model case falls through without a direct failure.

The result is a misleading split: an operator can have a syntactically valid, current config that cannot run an unattended agent. The scheduler eventually explains the problem, but only after work fails.

**Suggested action:** Add a shared, read-only “runtime readiness” resolver used by setup completion, `config check`, `doctor`, gateway startup, and cron creation. Report primary model, provider, credential source (never the secret), fallback availability, and which surfaces are runnable. Avoid a second independent validation schema. **Minimum model: sonnet.**

## Highest-value configuration changes for this instance

These are recommendations only; the audit did not apply them.

1. **Make the profile runnable:** persist the intended primary model/provider and one provider-diverse fallback. This is prerequisite work, not tuning.
2. **Stop noise before adding automation:** merge PR #63519 and purge only the confirmed fixture jobs. Then create a small, intentional cron portfolio; every agent job should name a model or inherit a verified default, load only the skills it needs, define a useful delivery target, and suppress “no change” output.
3. **Define “valuable” once:** use SOUL for voice/decision posture, USER.md for confirmed Colin/Ignite context, AGENTS.md for repo-local rules, and skills for repeatable workflows.
4. **Use a recommendation-first output contract:** lead with outcome/recommendation, separate evidence from inference, expose the decision or next action, and keep routine progress out of the final answer. This belongs in SOUL only at a general level; task-specific schemas belong in skills.
5. **Close the learning loop:** enable memory write approval during initial tuning, review proposed memories, and evaluate the first 20 real outputs against a short rubric (correct, actionable, appropriately concise, grounded, required little restating). Promote only stable patterns into skills/SOUL.

## Dead ends / things found clean

- **The built-in prompt is not the main weakness.** `agent/system_prompt.py` already assembles the prompt once per session and includes universal task-completion/no-fabrication guidance, parallel tool-call guidance, model-specific operational guidance, project context, memory, skills, and platform hints.
- **Prompt caching design is respected in the inspected configuration path.** Stable/context/volatile prompt tiers are built once and cached for the conversation; the audit found no configuration loader that re-renders the cached base prompt every turn.
- **Messaging display defaults are sensible.** `gateway/display_config.py` already defaults Telegram and Slack tool progress to `off`, hides reasoning, and keeps low-capability platforms final-answer-first. More display tuning is not a high-value first move.
- **Memory capability is enabled.** The problem is absent content and a dropped onboarding flow, not a disabled memory switch.
- **SOUL is editable in shipped UI surfaces.** Desktop/web profile editors already expose SOUL.md. The gap is guided operator-context creation and completion tracking, not lack of a raw editor.
- **The configuration loader is robust in several important ways.** It deep-merges defaults, caches by file signature, respects managed-scope overrides, backs up corrupt YAML, preserves sparse user configs, and keeps secrets out of `config.yaml` by design.
- **The July 10 setup audit remains separate context.** Its live-mini security/provider/backup findings were not duplicated here except where the current local configuration supplied new direct evidence.

## Method

The Codex runtime adapter requires sequential lenses when the user did not explicitly authorize subagents. Six distinct lenses were therefore run inline, followed by two focused extension checks and two adversarial refutation passes.

| Lens | Capability level | What it examined |
|---|---|---|
| Live configuration inventory | Mechanical | Raw config, auth metadata (secrets excluded), SOUL, memory files, skills, plugins, cron |
| Prompt/output quality | Judgment | System prompt composition, identity, completion guidance, display defaults |
| Resolution/readiness | Judgment | Model/provider precedence, cron model resolution, runtime logs |
| Validation/discoverability | Judgment | `config check`, `doctor`, structure validation, desktop/web settings |
| Personalization/learning | Judgment | USER/MEMORY defaults, write approval, first-contact profile build |
| Test/config integrity | Judgment | Pytest isolation, import-time paths, live fixture artifacts |
| Extension round 1 | Mechanical + judgment | Session/state aggregates and cron inventory; discarded weak session-output evidence |
| Extension round 2 | Judgment | Upstream PR/issue search; distinguished focused PR #63519 from unrelated draft #60882 |
| Adversarial verification ×2 | Judgment | Tried to refute H1 as user-created jobs and H2 as temporary runtime configuration |

Coverage was bounded to the standard-depth budget. No code, live config, job, memory, skill, plugin, session, or credential state was changed.
