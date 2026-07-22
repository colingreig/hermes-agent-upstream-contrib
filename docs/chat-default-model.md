# Interactive Hermes chat default model

**Status:** documents the current live default + fallback/mini strategy for
interactive Hermes chat (the main agent loop a user talks to directly — not
the cron/kanban auxiliary tasks, which have their own policy: see
`docs/model-routing.md` for content-creation cron jobs and
`hermes_cli/config.py` `DEFAULT_CONFIG["auxiliary"]` for the aux fallback
chains).

## Where the default actually lives

`hermes_cli/config.py`'s `DEFAULT_CONFIG["model"]` is intentionally an empty
string — this repo does **not** hardcode the interactive default. The real
value is a per-install setting in the live `~/.hermes/config.yaml`
(`model.default` / `model.provider`), set via `hermes model` or the setup
wizard, and resolved at runtime. Check it with:

```bash
ssh mini 'grep -A3 "^model:" ~/.hermes/config.yaml'
```

## Current recommended default (2026-07-22, prod-live-patches)

- **Default:** `gpt-5.6-sol` via `openai-codex` (ChatGPT Codex OAuth backend,
  `https://chatgpt.com/backend-api/codex`) — a full GPT-5.x reasoning model,
  appropriate for the tool-heavy, multi-step, long-context work interactive
  chat does. Do not default to a mini model here unless cost/latency is
  explicitly prioritized over quality.
- **Mini/cheap pair:** `gpt-5.4-mini` — used for cron/kanban profiles
  (summaries, classification, simple Q&A, fast helper turns) where a full
  reasoning model is unnecessary overhead. All cron/kanban profiles on the
  live mini already use this.
- **Model table:** `hermes_cli/codex_models.py::DEFAULT_CODEX_MODELS` lists
  `gpt-5.6-sol` first (the curated-fallback default shown when live Codex
  model discovery is unavailable), with `gpt-5.4-mini` immediately behind
  it — pinned by `tests/hermes_cli/test_codex_models.py::
  test_default_and_mini_chat_models_are_current_and_paired`.
- **Prompt caching / profile behavior:** unaffected by this policy — the
  default/mini choice is orthogonal to prompt-cache and per-profile config,
  neither of which this doc changes.

## Chat Completions vs Responses API

Hermes' OpenAI-Codex route already uses the Codex backend's own API shape
(not the plain Chat Completions endpoint), so no migration to the Responses
API was needed here.

## GPT-5.6 Sol resolved on this branch

`main` (dev fork) had flagged, but deliberately not acted on, OpenAI's
2026-07-09 GA of **GPT-5.6 Sol** as the new recommended flagship model,
superseding GPT-5.5 (released 2026-04-23) — a model-tier bump was treated as
a product/cost/quality call, not something to guess at from that branch.
`prod-live-patches` already carries this decision: its independent upstream
v0.18.2 merge (PR #70, predating this integration) landed `gpt-5.6-sol` as
`DEFAULT_CODEX_MODELS[0]`, so the bump is live on the mini today. `gpt-5.5`
remains a valid, current-generation GPT-5.x model further down the curated
list, not retired.
