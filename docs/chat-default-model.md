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

## Current recommended default (2026-07-22)

- **Default:** `gpt-5.5` via `openai-codex` (ChatGPT Codex OAuth backend,
  `https://chatgpt.com/backend-api/codex`) — a full GPT-5.x reasoning model,
  appropriate for the tool-heavy, multi-step, long-context work interactive
  chat does. Do not default to a mini model here unless cost/latency is
  explicitly prioritized over quality.
- **Mini/cheap pair:** `gpt-5.4-mini` — used for cron/kanban profiles
  (summaries, classification, simple Q&A, fast helper turns) where a full
  reasoning model is unnecessary overhead. All cron/kanban profiles on the
  live mini already use this.
- **Model table:** `hermes_cli/codex_models.py::DEFAULT_CODEX_MODELS` lists
  `gpt-5.5` first (the curated-fallback default shown when live Codex
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

## Open question for Colin — GPT-5.6 Sol

OpenAI's own current model docs (`developers.openai.com/api/docs/models`,
checked 2026-07-22) now name **GPT-5.6 Sol** (released 2026-07-09) as the
recommended flagship model, superseding GPT-5.5 (released 2026-04-23).
`gpt-5.5` is still a valid, current-generation GPT-5.x model — not
hallucinated or retired — but is no longer OpenAI's top recommendation as of
this writing. **Not bumped in this pass** (a model-tier change is a
product/cost/quality call, not something to guess at); confirm whether to
move the default (and `DEFAULT_CODEX_MODELS[0]`) to a GPT-5.6 tier once it's
available through the Codex OAuth backend.
