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

## Current deployed default (2026-07-22)

- **Default:** `gpt-5.6-sol` via `openai-codex` (ChatGPT Codex OAuth backend,
  `https://chatgpt.com/backend-api/codex`) — a full GPT-5.x reasoning model,
  appropriate for the tool-heavy, multi-step, long-context work interactive
  chat does. Do not default to a mini model here unless cost/latency is
  explicitly prioritized over quality.
- **Mini/cheap pair:** `gpt-5.4-mini` — used for cron/kanban profiles
  (summaries, classification, simple Q&A, fast helper turns) where a full
  reasoning model is unnecessary overhead. All cron/kanban profiles on the
  live mini already use this.
- **Model table:** `hermes_cli/codex_models.py::DEFAULT_CODEX_MODELS` includes
  both deployed policy options as an offline picker fallback. Live Codex
  discovery owns ordering and availability; the test guards membership and
  uniqueness instead of freezing a vendor-owned catalog snapshot.
- **Codex app-server authority:** Hermes resolves `model.default` for the
  active profile and sends that value in the stable `thread/start.model`
  field when `model.openai_runtime: codex_app_server` is selected, then
  repeats the current value in stable `turn/start.model`. That per-turn field
  makes an in-session `/model` switch effective without starting a new Codex
  thread or rebuilding its existing conversation history. Codex may still
  reroute an unavailable model for account/availability reasons, but it no
  longer silently substitutes `~/.codex/config.toml`'s default merely because
  the Hermes setting was omitted from the transport request.
- **Prompt caching / profile behavior:** unaffected by this policy — the
  default/mini choice is orthogonal to prompt-cache and per-profile config,
  neither of which this doc changes.

## Chat Completions vs Responses API

Hermes' OpenAI-Codex route already uses the Codex backend's own API shape
(not the plain Chat Completions endpoint), so no migration to the Responses
API was needed here.

## Deployed choice versus upstream recommendation

OpenAI's own current model docs (`developers.openai.com/api/docs/models`,
checked 2026-07-22) now name **GPT-5.6 Sol** (released 2026-07-09) as the
recommended flagship model, superseding GPT-5.5 (released 2026-04-23).
The deployed Hermes preference remains `gpt-5.5`, while OpenAI's upstream
recommendation is GPT-5.6 Sol. `gpt-5.5` remains a valid current-generation
GPT-5.x model; it is not a claim that it is the newest upstream
recommendation. Keeping it is deliberate until the newer tier is confirmed
available and suitable on the ChatGPT Codex OAuth route. Any future upgrade
should change the live profile preference after that validation, rather than
assuming the picker order controls a running app-server thread.
