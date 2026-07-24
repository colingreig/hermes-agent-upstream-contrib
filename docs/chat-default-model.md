# Interactive Hermes chat model strategy

**Scope:** documents the versioned interactive-chat baseline and its
full-model/mini strategy. It does not claim a volatile model choice for any
running installation. Cron and kanban auxiliary tasks have their own policy:
see `docs/model-routing.md` for content-creation cron jobs and
`hermes_cli/config.py` `DEFAULT_CONFIG["auxiliary"]` for the aux fallback
chains.

## Where the default actually lives

`hermes_cli/config.py`'s `DEFAULT_CONFIG["model"]` is intentionally an empty
string — this repo does **not** hardcode a deployment-specific interactive
default. A running Hermes instance resolves its value from that profile's
`~/.hermes/config.yaml` (`model.default` / `model.provider`), set via
`hermes model` or the setup wizard.

`tests/fixtures/chat-default-model.yaml` is the versioned baseline used by the
focused regression tests. It records a `gpt-5.5` / `openai-codex` interactive
configuration and proves that the configured value reaches the Codex app-server
transport. It is not evidence of what a live profile currently selects.

Audit a particular mini profile separately with:

```bash
ssh mini 'grep -A3 "^model:" ~/.hermes/config.yaml'
```

## Versioned interactive-chat baseline

<!-- chat-default-fixture:start -->
```yaml
model:
  default: gpt-5.5
  provider: openai-codex
```
<!-- chat-default-fixture:end -->

- **Configured baseline:** `gpt-5.5` via `openai-codex` (ChatGPT Codex OAuth backend,
  `https://chatgpt.com/backend-api/codex`) — a full GPT-5.x reasoning model,
  appropriate for the tool-heavy, multi-step, long-context work interactive
  chat does. Do not use a mini model as this baseline unless cost/latency is
  explicitly prioritized over quality.
- **Mini/cheap option:** `gpt-5.4-mini` remains available in
  `hermes_cli/codex_models.py::DEFAULT_CODEX_MODELS` for lower-risk work such
  as summaries, classification, simple Q&A, and fast helper turns. Individual
  cron and kanban profiles opt into their own configured model; this document
  does not infer their live values.
- **Model table:** the offline picker includes both baseline policy options.
  Live Codex discovery owns ordering and account availability; tests guard the
  configured full model and cheap option as membership/uniqueness invariants,
  not as a vendor-catalog snapshot.
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

## Baseline choice versus upstream recommendation

OpenAI's [current model guidance](https://developers.openai.com/api/docs/models)
recommends **GPT-5.6 Sol** as its flagship for complex reasoning and coding.
That upstream recommendation is distinct from this repository's `gpt-5.5`
fixture baseline. Hermes must not assume GPT-5.6 Sol is available through a
particular ChatGPT Codex OAuth account: verify the live Codex catalog and the
target profile before changing a deployed `model.default`. An upgrade changes
that profile's configuration; picker ordering never changes a running
app-server thread by itself.
