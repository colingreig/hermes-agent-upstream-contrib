---
title: "2026-07-29 Hermes fleet rebuild"
date: 2026-07-29
doc_type: fleet-changelog
---

# 2026-07-29 Hermes fleet rebuild

On 2026-07-29 we completed a gut-and-rebuild of the Hermes Mac mini fleet. The old configuration had grown into thirty-six overlapping cron jobs, duplicated monitors, and delegation-based subagent routing that once leaked a parent credential to a third-party endpoint. The rebuild replaces that sprawl with a curated fifteen-job cron set, five direct-execution profiles, and a governed installer that pins every source file before it writes anything to the mini.

## Why we rebuilt

The pre-freeze fleet mixed one-off experiments, retired email triage, duplicate executors, and standalone hygiene scripts that each owned their own schedule. Operators could not tell which jobs were load-bearing, which monitors were redundant, and which paths still pointed at deprecated delegation settings. The rebuild freezes the old state under `~/hermes-archive-20260729/`, clears the ClickUp rebuild board, and installs a single declarative bundle from `machine-setup/fleet-config/` (merged in PRs #210, #211, and #212). Eight jobs were dropped outright; three new consolidated jobs fold related checks into one LLM-orchestrated pass; thirteen kept jobs were carried forward with explicit rationale.

## Profiles and routing

Hermes runs five direct-execution profiles under `~/.hermes/profiles/`: `coder`, `content`, `design`, `research`, and `ops`. Each profile gets its own model config, SOUL persona, and bootstrap tree. The overlay deliberately clears the live `delegation` block so a named provider can never again sit beside a stray `delegation.base_url`. The `clickup-executor` and `content-lane-executor` jobs implement ClickUp work directly through their governed skills, post their handoff, and move the task to **In Review**. Only `ignite-validate` may mark work Complete. Hermes kanban remains available independently as a product feature.

## Model fallback and billing surfaces

The default agent fallback chain is now openai-codex `gpt-5.5` (Codex OAuth, subscription-covered), then zai `glm-4.7`, then nous `deepseek/deepseek-v4-pro` at reasoning effort `high`, backed by `NOUS_PORTAL_API_KEY` from 1Password (`op://hermes-agent/nous_portal_api_key/password`). That tier-3 choice superseded the earlier `moonshotai/kimi-k2.6` / OAuth-only design (86e2jzv2j). Google `gemini-2.5-flash` was removed after repeated false credential alarms from `.env` interpolation poison and an exhausted pool cache; it added a third billing surface without capability the remaining tiers lack. Billed `openai-api` remains banned everywhere in the bundle; Codex OAuth is the only sanctioned OpenAI surface. The **content profile is exempt by design**: it stays on `anthropic/claude-sonnet-5` with `fallback_providers: []`. Any model substitution on content work is a defect, not a graceful degradation.

## Governed install and release

`install_fleet_config.py` verifies sha256 hashes from `fleet_config_manifest.json`, snapshots every destination, writes atomically, and re-verifies deployed bytes. It deep-merges only four overlay keys into live `~/.hermes/config.yaml` (`model`, `fallback_providers`, `delegation`, `kanban`) so platforms, secrets wiring, approvals, and credential pool strategies stay untouched. `jobs.json` merges bundled definitions while preserving scheduler runtime fields for unchanged job ids (the initial rebuild cutover had no live runtime to carry forward). Production releases use manual governed cuts via `mini-release-cut.sh` with an exact certified SHA and immutable promotion receipt ID; the automatic release-poll LaunchAgent is retired for this fleet. Rollback paths are timestamped `.bak` siblings plus install receipts under `~/.hermes/logs/fleet-config-installs/`.

## What operators should expect

Day to day, you should see fewer cron entries firing independently, direct ClickUp work stopping at In Review until validation clears it, and the separate Hermes kanban feature available when explicitly needed. Consolidated jobs such as `clickup-lifecycle`, `fleet-health-digest`, and `repo-maintenance` batch what used to be separate one-off scripts. Monitor coverage is contract-driven via `fleet_outcome_probe.py` and `MONITOR_COVERAGE.md`. Tier-three Nous fallback uses DeepSeek V4 Pro high via `NOUS_PORTAL_API_KEY`. If something fails mid-install, the installer rolls back every step from its snapshot rather than leaving a half-written fleet behind.
