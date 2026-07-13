# Adversarial Review — Hermes Agent Configuration Audit

**Date:** 2026-07-13  
**Subject:** Adversarial review of `audit-hermes-agent-configuration-2026-07-13.md`, with particular attention to the two Needs-Colin decisions and whether the original recommendations improve real output.  
**Boundary:** Read-only on source code and every Hermes profile. No configuration, cron job, session, memory, credential, skill, or source file was changed.

## TL;DR

- The original cron-contamination finding survives, but it is one symptom of a broader **suite-wide test isolation failure** affecting `state.db`, config latches, logs, and likely other import-cached profile paths.
- The original claim that a missing default model had already harmed real scheduled work is **refuted**: the cited failures and sessions are test fixtures from pytest worktrees.
- This MacBook's default `~/.hermes` is not evidence of a poorly configured production agent; it is an uninitialized, test-polluted profile with no running local gateway or scheduler.
- Recommendation: treat the MacBook profile as development-only, fix pre-collection isolation, archive and purge the test artifacts, and evaluate output quality against the authoritative mini profile instead.
- **0 new ClickUp tasks:** the confirmed isolation work belongs in existing task `86e2a8mbh`; task `86e2a8mk4` should remain blocked until Colin decides whether a local agent is wanted.

## What changed under adversarial review

### High — CONFIRMED: the test-isolation defect is broader than cron

The original report correctly found 24 fixture-shaped cron jobs in the real default profile. Ten are enabled, although they are dormant because no local scheduler is running and no model is configured. The stronger finding is that the same isolation failure has polluted multiple profile surfaces:

- `~/.hermes/state.db` contains 230 sessions but only 12 messages. The messages exactly match test fixtures:
  - `do a tool-heavy task` and `Summary after compaction.` match `tests/run_agent/test_run_agent_codex_responses.py:1452-1485`.
  - `before restart` and `recover me` match `tests/gateway/test_session.py:1524-1547`.
  - 220 of the 230 session rows claim Telegram as their source, despite there being no evidence of a configured local Telegram agent.
- The three model-resolution failures cited in the original report came from `/hermes-agent/sucre/` test runs. `Check server status` matches the cron fixtures in `tests/cron/test_jobs.py` and `tests/hermes_cli/test_cron.py`; another nearby failure is explicitly named `no model anywhere`, matching `tests/cron/test_scheduler.py:2134`.
- `hermes_state.py:123` freezes `DEFAULT_DB_PATH = get_hermes_home() / "state.db"` at module import. `SessionDB()` uses that frozen path at `hermes_state.py:892` when no explicit path is passed.
- `gateway/run.py:1265` similarly freezes `_hermes_home` at import. That value is later used to persist onboarding latches, including `profile_build_offered` at `gateway/run.py:10954-10968`.
- `tests/conftest.py:334-363` redirects `HERMES_HOME` in an autouse fixture, which runs after pytest collection has already imported test modules and their dependencies.
- A mechanical sweep found many sibling import-time profile constants, including `cron/suggestions.py`, `run_agent.py`, `cli.py`, `tui_gateway/server.py`, checkpoint/session/skill tools, gateway caches, and provider auth paths. Rebinding only `cron.jobs` cannot prove the suite leaves the real profile untouched.

**Refutation attempt:** The alternative explanation is that Colin intentionally created sample sessions and jobs. It fails because the stored text, names, and schedules exactly match repository fixtures; log tracebacks identify temporary worktrees; and the repeated timestamps track test execution. The finding is confirmed.

**Recommended fix:** Establish a fail-safe suite `HERMES_HOME` before test collection (for example in pytest's pre-collection configuration hook), then retain per-test homes for isolation. Add an end-to-end guard that snapshots the real profile's config, cron, state, auth, memory, and cache paths before a representative multi-file run and proves they are unchanged afterward. PR [#63519](https://github.com/NousResearch/hermes-agent/pull/63519) is a good cron-specific reproduction, but its four-constant rebind is not sufficient for the demonstrated sibling paths. **Minimum model: sonnet.**

### High — CONFIRMED but DORMANT: fixture cron jobs remain a future activation hazard

The 24 jobs are still invalid state and should eventually be removed. The initial 1,464-starts-per-day figure is a maximum schedule rate, not present activity: no local gateway/scheduler process or Hermes launch agent is running, and every enabled job lacks a model. Enabling a default model before isolation and cleanup could activate meaningless work or side effects.

**Recommendation:** Fix isolation first, take a backup/snapshot of `~/.hermes`, classify entries by exact fixture signature, then remove only confirmed fixtures. Do not configure a persistent model before this sequence completes.

## Original findings challenged

### Original H2 — REFUTED as production harm; retained as a role decision

It remains factually true that the MacBook profile has no persisted model/provider. What does not survive is the claim that this caused three real scheduled tasks to fail: all cited failures came from pytest worktrees and fixture job names. An unconfigured profile with no running agent is not broken merely because it cannot run unattended work.

There is also no persisted Hermes credential source: `auth.json` has no providers and `~/.hermes/.env` is absent. Shell variables named `ANTHROPIC_API_KEY_HERMES` and `OPENAI_API_KEY_HERMES` are not evidence that the Hermes profile is deliberately configured or that those names are accepted by its provider registry.

**Recommendation:** Do not execute task `86e2a8mk4` until Colin decides whether the MacBook should host a real Hermes agent. If it is development-only, missing model readiness is desirable and the task should narrow to diagnostics. If it should be active, configure it only after isolation and cleanup.

### Original M1 — UNVERIFIED and currently NOT MEASURABLE

The stock SOUL and absent `USER.md`/`MEMORY.md` are real, but there is no trustworthy sample of real outputs to show that personalization is the limiting factor. Every stored message inspected is fixture text. The original recommendation to replace SOUL therefore jumped from absence to benefit without outcome evidence.

**Recommendation:** If a local agent is activated, keep the stock SOUL initially and add a compact `USER.md` containing confirmed operator facts plus a short recommendation-first preference. Evaluate 20 real outputs. Change SOUL only for stable cross-task behavior; promote repeated task shapes into skills only after they recur.

### Original M2 — UNDERLYING UX ISSUE VALID; local instance evidence unreliable

The code indisputably marks `profile_build_offered` before the user accepts or a fact is saved. That may be an intentional anti-nag policy rather than a completion tracker. The local latch cannot prove a user dropped out because `gateway/run.py` also caches the real profile path at import and tests have contaminated adjacent state.

**Recommendation:** Prefer a discoverable manual `profile build` replay action first. Add offered/completed/declined states only if product telemetry or support evidence shows users need automatic retries. This is smaller and avoids nagging users who intentionally ignored the first offer.

### Original M3 — PREMATURE for this profile

Custom Ignite skills can improve consistency, but building two or three before the agent has a role or real usage data risks speculative infrastructure. The authoritative mini already has a large operational skill surface, while this MacBook profile has no real sessions.

**Recommendation:** Do not add local skills yet. If the MacBook becomes active, start with one consumed workflow chosen from observed repetition; otherwise concentrate skill work on the mini.

### Original M4 — VALID PRODUCT GAP, lower immediate priority

`hermes config check` still does not answer whether an invocation can resolve a model. `hermes doctor` is stronger than the original report implied: it already fails a missing `.env`, validates configured providers/models, and checks credentials when a provider is present. It does not explicitly reject an absent model.

**Recommendation:** Add one read-only runtime-readiness check to `doctor` and reuse it from `config check` only if both commands are meant to promise runnability. Do not start with a new cross-surface framework spanning setup, gateway, and cron; the scheduler already has an appropriate fail-fast guard.

## Decision debate and recommendations

### 1. What should this MacBook profile be?

**Option A — Development-only (recommended):** Keep production Hermes on the mini, force every local test run into a pre-collection safe home, and remove the polluted local profile after backup. This prevents split-brain configuration and avoids paying to personalize an agent that is not running.

**Option B — A second real personal agent:** After isolation and cleanup, run the supported `hermes model` setup flow, create a small `USER.md`, and evaluate real outputs. A reasonable provider-diverse starting point from the repository's current catalog is Anthropic `claude-sonnet-5` as primary and OpenAI `gpt-5.5` as fallback, subject to confirming account access and cost posture.

**Rejected option — Share the mini's profile with local development:** This couples tests and production state, recreates the exact failure class, and makes output evidence untrustworthy.

### 2. How broad should the isolation fix be?

**Cron-only rebind:** Small and already proposed by PR #63519, but disproven as complete by the polluted `state.db` and onboarding/config paths.

**Pre-collection safe home plus per-test isolation (recommended):** Prevents any collection-time import from ever capturing the real profile. Per-test homes still prevent tests from contaminating one another. Targeted rebinding can remain for tests that require a fresh path after import.

**Production-wide dynamic path refactor:** Potentially cleaner, but higher risk because long-lived processes and profile scopes may deliberately bind at startup. Do not undertake this without a separate call-path audit.

### 3. What should be cleaned?

**Delete the 24 cron jobs only:** Too narrow; `state.db`, config latches, auth metadata, and logs are also test-derived.

**Archive then reset the whole local profile (recommended if development-only):** Take a timestamped backup, preserve anything not matching known fixtures for manual review, then rebuild a clean dev-only home. This is safer and easier to reason about than selectively editing several polluted stores.

**No cleanup:** Safe only while nothing starts. It leaves future agents and audits unable to distinguish user intent from fixtures.

## Questions for Colin

1. **Profile role:** Confirm that this MacBook `~/.hermes` should be development-only, with the mini remaining the authoritative live Hermes. **Recommendation: yes.**
2. **Cleanup authority:** After the isolation guard lands, approve a timestamped backup followed by removal/reset of the test-polluted local profile, preserving any non-fixture artifact for review. **Recommendation: approve.**
3. **Task scope:** Broaden ClickUp task `86e2a8mbh` from cron-only isolation to suite-wide pre-collection `HERMES_HOME` safety plus profile-integrity regression coverage. **Recommendation: broaden.**
4. **Conditional model choice:** If the answer to question 1 is no and a local agent is wanted, approve using Anthropic `claude-sonnet-5` primary with OpenAI `gpt-5.5` fallback, configured through Hermes' supported setup flow after cleanup. **Recommendation: use this provider-diverse pair initially, then evaluate 20 outputs.**

## Dead ends / things found clean

- No local Hermes gateway, scheduler, or launch agent is running; the cron schedule rate is latent, not current spend.
- The built-in prompt and cache-stability conclusions from the original audit still hold.
- The cron scheduler's missing-model failure is correct behavior; it prevented polluted jobs from executing.
- `hermes doctor` already performs meaningful env/provider/credential validation; the missing-model gap does not justify replacing it.
- The mini's live production configuration was not re-audited in this pass. The existing July 10 report remains the durable evidence that the mini—not this profile—is authoritative.
- No upstream issue or PR was found for the broader `state.db` isolation defect. PR #63519 addresses only `cron.jobs` cached paths.

## Method

The Codex runtime adapter requires sequential lenses when the user does not explicitly authorize subagents. The review therefore ran inline as distinct checks.

| Lens | Capability | Result |
|---|---|---|
| Evidence provenance | Judgment | Traced cited model failures to pytest worktrees and fixture names |
| Live-state integrity | Mechanical | Counted 230 sessions/12 messages and matched all message text to tests |
| Activation/reachability | Judgment | Confirmed no local scheduler/gateway; cron risk is dormant |
| Isolation architecture | Judgment | Mapped fixture timing and sibling import-cached profile paths |
| Recommendation proportionality | Judgment | Reduced personalization, onboarding, skills, and diagnostics proposals |
| Prior-audit reconciliation | Judgment | Distinguished the MacBook test profile from the authoritative mini |
| Upstream search | Mechanical | Confirmed PR #63519 is cron-specific; found no broader state isolation fix |
| Adversarial refutation | Judgment | Refuted original H2 harm claim; confirmed broader test pollution |

Coverage was bounded to one focused adversarial pass plus one extension round. No source or Hermes runtime state was changed.
