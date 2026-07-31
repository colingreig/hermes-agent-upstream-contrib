# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the
`hermes-agent` codebase.

Never give up on the right solution.

## What Hermes Is

Hermes is a personal AI agent that runs the same agent core across a CLI, a
messaging gateway (Telegram, Discord, Slack, and many other platforms), a TUI,
and an Electron desktop app. It learns across sessions through memory and
skills, delegates to subagents, runs scheduled jobs, and drives a real terminal
and browser. It is extended primarily through plugins and skills, not by
growing the core.

Two properties shape almost every design decision:

- Per-conversation prompt caching is sacred. A long-lived conversation reuses a
  cached prefix every turn. Anything that mutates past context, swaps toolsets,
  or rebuilds the system prompt mid-conversation invalidates that cache and
  multiplies the user's cost. Do not do it. Context compression is the sole
  exception.
- The core is a narrow waist; capability lives at the edges. Every model tool
  we add is sent on every API call, so the bar for a new core tool is high.
  Most new capability should arrive as a CLI command plus skill, a
  service-gated tool, a plugin, or an MCP server instead of new core surface.

Unless the user says otherwise, finish repository work end to end: implement,
validate, commit, push, and open the normal PR or review handoff. Do not leave
loose ends such as uncommitted changes, unpushed branches, missing validation,
or silent blockers. Preserve unrelated work already present in a checkout.

Read the nearest scoped `AGENTS.md` and relevant docs before changing a
specialized surface. The filesystem is authoritative; do not rely on stale file
counts, old code tours, or recalled paths.

## Required Homes And Links

This root file is the quick operating contract. Detailed, durable homes live
here:

- Contributor policy and review intent: [CONTRIBUTING.md](CONTRIBUTING.md) and
  [Contribution Rubric](website/docs/developer-guide/contribution-rubric.md).
- Testing policy: [testing-conventions](website/docs/developer-guide/testing-conventions.md).
- TUI scope: [ui-tui/AGENTS.md](ui-tui/AGENTS.md).
- Plugin architecture: [plugins/index](website/docs/developer-guide/plugins/index.md).
- Built-in tool authoring: [adding-tools](website/docs/developer-guide/adding-tools.md).
- Agent loop internals: [agent-loop](website/docs/developer-guide/agent-loop.md).
- Desktop scope: [apps/desktop/AGENTS.md](apps/desktop/AGENTS.md), when present
  in the checkout.

If guidance is too long for root, keep a concise rule here and move the
operational detail to one of those homes. Do not delete unique guidance without
leaving a written home and a root link.

## Contribution Rubric

This is the project's intent layer. Use it two ways:

1. For contributors and maintainers: aim changes at what Hermes actually wants
   to merge.
2. For automated review and triage: recognize when a PR is safe to close for an
   allowed mechanical reason, and when a human maintainer should decide.

Hermes ships a lot. Bug fixes, new platforms, new providers, and product
surface work are welcome. The restraint below applies most strongly to the core
agent waist and the model tool schema, because every core tool and
prompt-shaping decision is paid for on every model request.

### What We Want

Fix real bugs, well. The strongest contribution starts from a real symptom,
reproduces it on current `main`, identifies where the behavior manifests, and
fixes the whole bug class. Look for sibling call paths, async variants,
gateway/CLI parity, profile variants, and remote-backend versions of the same
issue.

Expand reach at the edges. New platform adapters, messaging channels,
providers, models, Desktop/TUI features, and dashboard support panels are
welcome when they integrate through the existing setup/config/plugin surfaces.
Breadth is a product goal. The question is how the capability is wired, not
whether Hermes is allowed to grow.

Refactor god-files into focused modules. Large mechanical extractions from
`cli.py`, `run_agent.py`, and `gateway/run.py` are valid work when the request
is the extraction. Keep the new module boundary honest, preserve behavior, and
validate the real path after the move.

Keep the core narrow. New model tools are the expensive exception because every
model-tool schema is sent on every API call. Prefer the Footprint Ladder:

1. Extend existing behavior.
2. Add a CLI command plus a skill.
3. Add a service-gated tool with `check_fn`.
4. Add a plugin.
5. Add an MCP server in the catalog.
6. Add a core tool only when the capability is fundamental and unreachable by
   terminal, file, skill, plugin, or MCP surfaces.

Extend, do not duplicate. Before adding a manager, hook, provider system, or
module, check whether an existing surface already covers it. When several PRs
integrate the same category of thing, design one shared interface and wrap the
existing built-in as the first provider rather than merging competing one-off
integrations.

Use behavior contracts over snapshots. Tests should assert how pieces of data
relate, not freeze today's catalog, config version, or enum count. See
[testing-conventions](website/docs/developer-guide/testing-conventions.md) for
concrete examples of invariant tests and rejected change-detector tests.

Run E2E validation for risky paths. Anything touching resolution chains,
configuration propagation, security boundaries, remote backends, persistent
state, file I/O, network I/O, or tool dispatch needs real-path validation with
actual imports against an isolated `HERMES_HOME`. Mocks are useful for narrow
branches, but they do not prove the integration works.

Keep changes cache-, alternation-, and invariant-safe. Preserve prompt caching,
strict message role alternation, and byte-stable system prompt construction for
the life of a conversation. New capabilities should not rebuild the system
prompt or swap the active toolset mid-session unless the change is explicitly a
context-compression or new-session boundary.

Preserve contributor credit. When incorporating external work, salvage it where
feasible through cherry-pick/rebase-merge style flows so authorship survives in
history. Do not reimplement from scratch just because it is faster locally.

### What We Reject

Speculative infrastructure. Hooks, callbacks, and extension points with no
concrete consumer are rejected. Adding a hook is cheap; removing one after
plugins depend on it is expensive. A hook is not speculative when a real
contributor has a stated use case, even if the consumer ships separately.

New non-secret `HERMES_*` user config. `.env` is for credentials only: API keys,
tokens, passwords, and similar secrets. Behavioral settings belong in
`config.yaml`. If an implementation needs an internal environment variable as a
bridge, keep user-facing docs and setup flow pointed at `config.yaml`.

A new core tool when terminal, file, skill, plugin, or MCP works. If the only
barrier is file visibility on a remote backend, fix the mount or backend
visibility. If behavior can be spelled as repeatable instructions and commands,
make a skill. If it is niche or user-specific, make a plugin or MCP server.

Lazy-reading escape hatches on instructional tools. Do not add pagination like
`offset` or `limit` to tools that load instructions the agent must read fully,
such as skills, prompts, or playbooks. Agents will often read page one and skip
the rest. Keep instructional reads complete.

"Fixes" that destroy the feature they secure. A mitigation that disables the
feature's purpose is the wrong mitigation. Read the original intent before
restricting behavior, then find a narrower fix that keeps the feature useful.

Outbound telemetry without opt-in gating. Do not add analytics, third-party
identifier tagging, usage attribution, or vendor telemetry until a generic
user-facing opt-in exists, including config gate, setup prompt, and a
`hermes tools` or equivalent toggle.

Plugins that touch core files for product-specific behavior. Plugins live in
plugin directories and use generic plugin APIs. If a plugin needs a broader
surface, widen the generic plugin surface. Do not special-case a specific
plugin in the core tree.

Third-party products integrated into the core tree. Observability backends,
vendor SaaS connectors, analytics dashboards, and other "someone else's
product" integrations belong in standalone plugin repos. Users install them
into `~/.hermes/plugins/` or through a package entry point. This is a coupling
and maintenance policy, not a quality judgment.

### Verify the Premise Before Calling It a Bug

The most common reason a well-written PR gets closed is not code quality. It is
that the change is built on a wrong premise or treats intentional design as a
gap. These case studies are review guardrails. The durable version lives in the
[Contribution Rubric](website/docs/developer-guide/contribution-rubric.md).

Intentional design, not a gap. Profiles are independent islands on purpose. A
change that adds live inheritance from the default profile looks helpful, but
it couples profiles in exactly the way the design avoids. The legitimate "start
from my default" workflow is a copy-at-creation path such as `--clone`, not
ongoing inheritance. Before filling a missing link, ask whether the isolation
is the design.

Wrong mental model of existing behavior. A rate-limit PR proposed re-probing
during cooldown. The premise was wrong: the breaker only trips on a
confirmed-empty account bucket, so re-probing simply hammers a bucket already
proven empty. Another usage-accumulation fix added a branch that never executed
because an earlier guard already popped the state it depended on. Trace the
real call path before accepting the rationale.

An omission was load-bearing. Restoring "missing" `__init__.py` files once made
a test tree importable as a dotted package that shadowed the real plugin and
deleted its `register()` at import time. The absence looked accidental. It was
protecting import behavior. When a fix adds the obvious missing file, import,
config layer, or fallback, check whether the omission prevents shadowing,
cycles, cache invalidation, or incorrect auto-discovery.

Resurrecting a rejected direction. Some PRs work technically but revive an
approach the maintainers intentionally moved away from, or supersede a narrower
agreed base. Keep the change scoped to the piece that was requested. Offer
broader ideas as focused follow-ups instead of bundling them into the current
PR.

Before you approve, merge, or mechanically close a PR, reproduce the symptom or
verify the claimed gap against current `main`; identify the runtime line or
path where the fix acts; place the capability on the smallest viable Footprint
Ladder rung; confirm tests are behavior/invariant tests; and exercise the real
integration path for config, state, security, backend, file, network, or tool
dispatch changes.

## Development Environment

Prefer `.venv`; fall back to `venv` if that is what the checkout has:

```bash
source .venv/bin/activate
# or
source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv` first, then `venv`, then
`$HOME/.hermes/hermes-agent/venv` for worktrees that share a venv with the main
checkout.

User config is `~/.hermes/config.yaml` for settings and `~/.hermes/.env` for
API keys only. Logs live in `~/.hermes/logs/`: `agent.log`, `errors.log`, and
`gateway.log` when the gateway runs. Use `hermes logs [--follow] [--level ...]
[--session ...]`.

## Project Structure

File counts shift constantly. Treat this as a map of load-bearing entry points,
not an exhaustive tree.

```text
hermes-agent/
├── run_agent.py          # AIAgent class and core conversation loop
├── model_tools.py        # Tool orchestration and handle_function_call()
├── toolsets.py           # Toolset definitions and _HERMES_CORE_TOOLS
├── cli.py                # HermesCLI interactive CLI orchestrator
├── hermes_state.py       # SessionDB SQLite session store
├── hermes_constants.py   # get_hermes_home(), display_hermes_home()
├── hermes_logging.py     # setup_logging()
├── batch_runner.py       # Parallel batch processing
├── agent/                # Provider adapters, memory, caching, compression
├── hermes_cli/           # CLI subcommands, setup, plugins loader, skins
├── tools/                # Built-in tool implementations
│   └── environments/     # Terminal backends
├── gateway/              # Messaging gateway and platform adapters
├── plugins/              # Plugin system and bundled plugin examples
├── optional-skills/      # Heavier/niche skills not active by default
├── skills/               # Built-in skills bundled with the repo
├── ui-tui/               # Ink terminal UI; see ui-tui/AGENTS.md
├── tui_gateway/          # Python JSON-RPC backend for TUI/Desktop
├── apps/desktop/         # Electron desktop chat app
├── acp_adapter/          # ACP server
├── cron/                 # Scheduler
├── scripts/              # Test/release/install helpers
├── website/              # Docusaurus docs site
└── tests/                # Pytest suite
```

## File Dependency Chain

The built-in tool path is intentionally narrow:

```text
tools/registry.py
       ↑
tools/*.py
       ↑
model_tools.py
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

Tool modules call `registry.register()` at import time. Discovery makes the
tool known to the registry, but agents see it only when it is included in a
toolset. See [adding-tools](website/docs/developer-guide/adding-tools.md) and
[agent-loop](website/docs/developer-guide/agent-loop.md).

## Core Invariants

### Prompt-Cache Stability

A long-lived conversation reuses a cached prompt prefix. Do not mutate its
past context, tool set, memories, or system prompt mid-conversation. Context
compression is the only exception. New slash commands or settings that affect
prompt state should take effect next session by default, with an explicit
immediate-invalidation option only when necessary.

Keep message-role alternation valid. Do not inject synthetic user messages
mid-loop or create adjacent messages with the same role. Tool messages may be
consecutive only as tool-call results following the assistant tool-call
message.

### Narrow Core, Capable Edges

Every core model-tool schema is sent on every model request. Use the Footprint
Ladder before adding model-visible surface. Most custom capability belongs in a
skill, plugin, MCP server, provider plugin, or service-gated tool. A core tool
must be broadly fundamental and unreachable through existing surfaces.

Do not add speculative hooks or frameworks. A plugin must use a generic,
existing extension surface; do not hard-code plugin-specific behavior into
core. New integrations for third-party products belong in standalone plugins,
not new in-tree product directories.

### Config And State

Hermes supports isolated profiles. Use `get_hermes_home()` for persistent state
and `display_hermes_home()` in user-visible paths; do not hard-code
`~/.hermes` or `Path.home() / ".hermes"` in profile-scoped code. Tests must
keep state in a temporary `HERMES_HOME`; profile tests also need to control
`Path.home()` where profile-root behavior is under test.

User behavior belongs in `config.yaml`; `.env` is for credentials and other
secrets only. Do not introduce a user-facing `HERMES_*` environment variable
for non-secret configuration. Put user state under the active Hermes home and
keep schemas/descriptions profile-aware.

### Security And Approval Boundaries

Do not weaken credential handling, approval gates, sandboxing, redaction, or
network boundaries to make a test pass. If a feature needs an exception, make
the user-facing control explicit and default-safe. Pin Git sources and GitHub
Actions to immutable commit SHAs. Use bounded dependency versions.

Do not add outbound telemetry, usage attribution, vendor identifiers, or
analytics without explicit opt-in plumbing. If a provider requires metadata for
functionality, document what is sent and gate it appropriately.

## AIAgent Class And Agent Loop

The real `AIAgent.__init__` in `run_agent.py` takes many parameters:
credentials, routing, callbacks, session context, budget, credential pool,
checkpoints, prefill messages, service tier, reasoning config, and more. Read
the source before editing the constructor or call sites.

Common entry points:

```python
agent.chat(message: str) -> str
agent.run_conversation(
    user_message: str,
    system_message: str | None = None,
    conversation_history: list | None = None,
    task_id: str | None = None,
) -> dict
```

The loop is synchronous and tool-calling oriented:

```text
while under iteration budget:
    build provider messages from session history
    call the provider
    if response has tool calls:
        execute tools
        append tool result messages
        continue
    else:
        persist and return final response
```

All messages use OpenAI-style internal format:

```python
{"role": "system", "content": "..."}
{"role": "user", "content": "..."}
{"role": "assistant", "content": "...", "tool_calls": [...]}
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

Reasoning content is stored in `assistant_msg["reasoning"]`. The agent loop
intercepts some stateful tools (`todo`, `memory`, `session_search`,
`delegate_task`) before registry dispatch because they need session-local
agent state.

For details, use [agent-loop](website/docs/developer-guide/agent-loop.md).

## Built-In Tools

Before adding a built-in tool, ask whether this should be a skill, plugin, MCP
server, provider plugin, or extension to an existing tool. Built-in tools are
core surface and should be rare.

When a built-in tool is justified:

- Add a focused `tools/<name>.py` with handler, schema, `check_fn` when needed,
  and `registry.register()`.
- Add the tool name to `_HERMES_CORE_TOOLS` or a specific toolset in
  `toolsets.py`.
- Return JSON strings from handlers; report errors as `{"error": "..."}` rather
  than raising through the agent loop.
- Use `check_fn` so unavailable service-backed tools are hidden from the model.
- Test through the real tool-definition and dispatch path, not only the helper.

The canonical procedure lives at
[adding-tools](website/docs/developer-guide/adding-tools.md).

## Plugins And Skills

Plugins and skills are the normal way to grow Hermes without widening the core.
Use [plugins/index](website/docs/developer-guide/plugins/index.md) for plugin
authoring and routing to provider-specific plugin docs.

Plugin rules:

- Custom tools, hooks, slash commands, CLI subcommands, and bundled skills
  belong in plugins when they are personal, project-local, niche, or
  third-party-specific.
- Plugins use generic `ctx` registration APIs. If a plugin needs a missing
  capability, widen the generic surface rather than special-casing that plugin.
- Third-party product integrations ship as standalone plugin repos, not new
  in-tree product directories.
- Plugin handlers should be thread-safe. Lazy singletons need locking helpers,
  not ad hoc global `None` checks.
- Optional dependencies should be lazy and gated. Bundled lazy installs use the
  in-tree allowlist; standalone plugins should declare package extras.

Skills are operational procedures. When a user names a skill, load and follow
its `SKILL.md` before acting; do not substitute a handwritten approximation.
Instructional skill reads must be complete, not paginated.

## CLI Architecture

`cli.py` owns the classic interactive CLI. It uses Rich for banners and panels,
`prompt_toolkit` for input and autocomplete, `KawaiiSpinner` for activity, and
the skin engine in `hermes_cli/skin_engine.py` for data-driven CLI theming.

Slash commands are centralized in `hermes_cli/commands.py`. Add a
`CommandDef`, then route through `HermesCLI.process_command()` and, when
available in messaging, `gateway/run.py`. The registry drives CLI help,
gateway known commands, Telegram command menus, Slack subcommand routing,
autocomplete, and category help.

Adding an alias should only require changing the `aliases` tuple on the
existing `CommandDef`. If you find yourself updating several command lists by
hand, stop and use the central registry.

Skill slash commands are scanned from skills and injected as user messages,
not system prompt text, to preserve prompt caching.

## Gateway Architecture

`gateway/` owns messaging platforms. Platform adapters live in
`gateway/platforms/`; shared session logic lives in `gateway/session.py`;
`gateway/run.py` orchestrates commands, platform events, and agent calls.

Gateway changes usually need real-path validation because they cross async
delivery, platform formatting, session persistence, and profile boundaries.
Do not assume a CLI-only test validates a messaging behavior. Keep command
behavior aligned with the central slash-command registry.

Gateway hooks and shell hooks are extension points. Do not add always-on
special cases for one integration when a generic hook or plugin surface is the
right home.

## TUI Architecture

The TUI is a full replacement for the classic `prompt_toolkit` CLI, activated
via `hermes --tui` or `HERMES_TUI=1`. It is an Ink/React app in `ui-tui/` with
a Python JSON-RPC backend in `tui_gateway/`.

```text
hermes --tui
  └─ Node (Ink) --stdio JSON-RPC-- Python (tui_gateway)
       │                              └─ AIAgent + tools + sessions
       └─ renders transcript, composer, prompts, activity
```

TypeScript owns the screen. Python owns sessions, tools, model calls, and
slash-command logic. Read [ui-tui/AGENTS.md](ui-tui/AGENTS.md) before TUI or
`tui_gateway` work.

Common TUI commands:

```bash
cd ui-tui
npm install
npm run dev
npm run build
npm run typecheck
npm run lint
npm test
```

## Dashboard Chat Boundary

The dashboard embeds the real `hermes --tui` through a PTY. It does not
reimplement the transcript or composer in React. Browser code mounts xterm.js
and connects to `/api/pty`; the server spawns the same TUI backend a terminal
user would run.

Do not build a second React chat transcript or composer for the dashboard.
Extend Ink so the dashboard gets the behavior automatically. Structured React
UI around the TUI is allowed only when it complements the embedded terminal:
sidebars, inspectors, summaries, status panels, and similar supporting views.
Keep those failures non-destructive so the terminal pane remains usable.

## Electron Desktop App

`apps/desktop/` is a separate Electron + React chat surface. It does not embed
the dashboard or TUI. It talks to a headless Hermes backend over JSON-RPC using
shared transport code, and it has its own composer, transcript, slash-command
pipeline, renderer state, and Electron process boundary.

For Desktop work, read `apps/desktop/AGENTS.md` and `apps/desktop/DESIGN.md`
when present. Follow the TypeScript style rules below. Do not add a build or
runtime dependency from Desktop to the dashboard frontend.

Desktop slash commands are curated client-side but dispatched to the backend.
The curation hides terminal-only, messaging-only, picker-owned, settings-owned,
and advanced built-ins. It must not hide user-activated extensions: skill
commands and quick commands surfaced by the backend belong in completions and
execution.

## TypeScript Style

Applies to TypeScript across Hermes: Desktop, TUI, website, and future TS
packages.

- Prefer small nanostores over component state when state is shared, reused, or
  read by distant UI.
- Let each feature own its atoms. Chat state belongs near chat, shell state near
  shell, shared state in `src/store`.
- Components that render from an atom should use `useStore`. Non-rendering
  actions should read with `$atom.get()`.
- Do not pass state through three components when the leaf can subscribe to the
  atom.
- Keep persistence beside the atom that owns it.
- Keep route roots thin. They compose routes and shell; they should not become
  controllers.
- No monolithic hooks. A hook should own one narrow job.
- Prefer colocated action modules over hidden god hooks.
- If a callback is pure side effect, use the terse void form:
  `onState={st => void setGatewayState(st)}`.
- Async UI handlers should make intent explicit:
  `onClick={() => void save()}`.
- Prefer interfaces for public props and shared object shapes. Avoid
  `type X = { ... }` for object props.
- Extend React primitives for props: `React.ComponentProps<'button'>`,
  `React.ComponentProps<typeof Dialog>`, `Omit<...>`, `Pick<...>`.
- Table-driven beats condition ladders when mapping ids, routes, or views.
- `src/app` owns routes, pages, and page-specific components.
- `src/store` owns shared atoms.
- `src/lib` owns shared pure helpers.

## Testing And Verification

Use the repository test wrapper, not direct `pytest`:

```bash
scripts/run_tests.sh
scripts/run_tests.sh tests/gateway/
scripts/run_tests.sh tests/agent/test_foo.py::test_x
```

The wrapper selects `.venv`, then `venv`, then the shared Hermes venv and runs
tests in a hermetic environment: temp home, credential vars cleared, UTC, stable
locale, subprocess isolation, and CI-like parallelism. Direct `pytest` can pass
locally while CI fails, or fail locally because your real provider credentials
and home directory leaked into the run.

For TypeScript packages, run the package's documented commands from that
package. A Python test run does not validate a frontend change. For Desktop,
follow its scoped checks. For TUI, use the `ui-tui` commands above.

Run the smallest relevant test first, then broader validation proportionate to
risk. Report any check that cannot run and why.

Do not write change-detector tests. A test that freezes a model catalog entry,
config version literal, enum count, or provider list length adds no behavioral
coverage. It turns routine updates into CI failures. Assert relationships and
invariants instead.

Do not read source code text in tests. Source-regex tests verify formatting and
shape, not runtime behavior. Extract logic into a small function and call it
for real. If extraction feels disruptive because logic is buried in a god-file,
that is a signal to extract it, not to regex around it.

Full examples and policy live in
[testing-conventions](website/docs/developer-guide/testing-conventions.md).

## Development And Change Hygiene

Make the smallest coherent change that satisfies the request. Read the code
path and its callers before treating a behavior as a bug; some absences and
restrictions are intentional. Preserve contributor authorship when integrating
existing work. Leave unrelated refactors and metadata churn alone unless they
are needed for safety.

Prefer existing local patterns, helper APIs, and ownership boundaries. Use
structured APIs and parsers instead of ad hoc string manipulation where the
standard toolchain or codebase offers them. Add an abstraction only when it
removes real complexity, reduces meaningful duplication, or matches an
established local pattern.

When you introduce a new symbol that is meant to enforce behavior, verify that
production code calls it. A guard, helper, validation function, UI action, or
server handler is not live until it is wired into the real decision point. Use
search to confirm non-test, non-definition call sites before claiming the fix is
done.

Before delivery, check that the implementation is scoped, profile-safe,
cache-safe, security-safe, and tested on the affected surface. Summarize the
change, tests, and any remaining limitation in the handoff.

## Surface-Specific Validation

Use the surface that actually owns the behavior. A green test in a neighboring
surface can be useful signal, but it is not proof that the changed path works.

Agent loop changes usually need at least one test that drives
`AIAgent.run_conversation()` or the relevant transport adapter with real
message objects. Preserve role alternation, tool-call/result pairing, reasoning
field persistence, session persistence, compression boundaries, and fallback
behavior. If a provider-specific mode is affected, exercise that adapter's
format conversion rather than only the OpenAI-compatible happy path.

Tool runtime changes need real registry/toolset coverage. Discovery of
`tools/*.py` is only the first step; the model sees a tool only when the
toolset includes it and its `check_fn` passes. Test unavailable-service paths
as well as available paths, and make sure errors return JSON strings instead of
escaping through the loop.

CLI command changes need registry coverage, handler coverage, help or
autocomplete coverage where applicable, and a check that aliases resolve
through `resolve_command()`. If the command should exist in the gateway, verify
gateway dispatch too. If the command is CLI-only or gateway-gated, encode that
with `CommandDef` fields rather than an undocumented branch.

Gateway changes need async/event-path validation. Exercise platform formatting,
session lookup, command dispatch, progress messages, and any retry or rate-limit
path touched by the change. Keep platform adapters thin and shared gateway
logic central. Do not make one platform's workaround the default behavior for
all platforms unless the shared contract really changed.

TUI changes belong in `ui-tui/` and `tui_gateway/`. Run TypeScript checks from
`ui-tui` and Python checks for backend changes. If behavior affects slash
commands, completions, approvals, prompts, or streaming, verify the JSON-RPC
method/event boundary. Read [ui-tui/AGENTS.md](ui-tui/AGENTS.md) first.

Dashboard chat changes must respect the PTY boundary. If the task is about the
main transcript, composer, slash-command behavior, or terminal interaction,
extend Ink/TUI. React dashboard code may provide surrounding panels, inspectors,
and controls, but it must not become a second primary chat surface.

Desktop changes belong under `apps/desktop/` and the shared transport package.
Desktop is not a dashboard wrapper and not a TUI embed. Validate renderer state,
Electron process behavior, backend spawning, and JSON-RPC calls according to
the Desktop scoped guide. Keep user extension slash commands flowing through
Desktop curation.

Plugin changes need discovery, enablement, and failure-mode checks. A plugin
that fails to load should log clearly and let Hermes continue. A plugin tool
should accept `**kwargs`, return JSON strings, and not assume single-threaded
execution. A plugin hook should tolerate future keyword arguments and should
not block the agent loop on best-effort observer work.

Provider and model-routing changes need behavior contracts rather than catalog
snapshots. Assert that models have required metadata, that routing precedence is
honored, that fallbacks trigger for the right classes of errors, and that auth
refresh or credential-pool behavior does not leak between profiles.

Cron and scheduler changes need isolated `HERMES_HOME` tests and careful time
control. Do not rely on local timezone, wall-clock races, real user cron state,
or real secrets. If a job can run under a different profile, verify profile home
resolution and log routing.

Security and credential changes need negative controls. Prove the allowed case
still works and the forbidden case fails closed. Redaction, approval prompts,
sandboxing, network egress controls, secret-source lookup, and profile
isolation are behavior contracts, not implementation preferences.

## Command And Configuration Rules

All slash commands start in `hermes_cli/commands.py`. The central registry is
the source for CLI help, autocomplete, gateway known commands, gateway help,
Telegram command menus, and Slack subcommand routing. Add aliases in the
registry. Do not maintain hidden parallel command lists.

Use `save_config_value()` or the appropriate config helper for persistent
settings. Settings that affect behavior belong in `config.yaml`; secrets belong
in `.env` or a secret-source provider. If setup needs to prompt for a value,
route it through the setup/config machinery so docs, defaults, validation, and
profile behavior stay aligned.

Do not make a raw environment variable the public interface for a behavioral
setting. Internal environment variables are acceptable only as implementation
bridges, and they should be derived from config or setup state. Documentation
should teach the config key, not the private bridge.

When a setting changes prompt construction, memory sources, toolsets, or other
cached-prefix content, default it to next-session application. Immediate
application must be explicit and should say that it invalidates the current
conversation cache.

When adding dependencies, use bounded versions. Git dependencies and GitHub
Actions must be pinned to immutable SHAs. Optional heavy dependencies should be
lazy, feature-gated, or plugin-scoped rather than installed for every user.

## Review And Triage Closure Rules

Automated or semi-automated PR triage may close only for narrow mechanical
reasons such as `implemented_on_main`, `cannot_reproduce`, or `incoherent`.
Taste-based "we do not want this" decisions stay with human maintainers.

For `implemented_on_main`, prove the behavior exists on current `main` through
source and, when feasible, runtime evidence. Similar code is not enough; the
reported user-visible behavior must be covered. If the PR adds tests for a
bug already fixed on main, consider whether the tests are still valuable rather
than closing reflexively.

For `cannot_reproduce`, run the stated reproduction or a faithful equivalent in
an isolated environment. If the report lacks enough detail to reproduce but the
claim is plausible, ask for clarification or leave it for a human. Do not close
because local setup is missing optional credentials, services, or a platform
the issue explicitly depends on.

For `incoherent`, the change must be genuinely impossible to evaluate: missing
files, contradictory claims, generated junk, or no discernible relation to
Hermes. A rough but understandable contribution is not incoherent merely
because it is incomplete or stylistically awkward.

When a PR is built on a wrong premise, cite the exact code path or runtime
behavior that disproves the premise. When a PR fights intentional design, cite
the design home or history. If neither is clear, leave it open for a human
maintainer.

Never use this rubric to convert maintainers' taste into an automated close.
The rubric exists to avoid bad closes as much as to catch bad changes.

## Documentation Placement

Root `AGENTS.md` should stay concise enough to load as workspace context, but
not so small that unique operational rules disappear. Durable homes:

- Contribution intent and case studies: `website/docs/developer-guide/contribution-rubric.md`
- Testing examples and anti-patterns: `website/docs/developer-guide/testing-conventions.md`
- Built-in core tool procedure: `website/docs/developer-guide/adding-tools.md`
- Agent loop details: `website/docs/developer-guide/agent-loop.md`
- Plugin authoring and routing map: `website/docs/developer-guide/plugins/index.md`
- TUI scoped process model: `ui-tui/AGENTS.md`
- User-facing contributor entrypoint: `CONTRIBUTING.md`

If a future cleanup removes detail from this file, it must first create or
identify the durable home and leave an explicit root link. Do not delete a
rule because it feels too specific; move it to the owning doc and keep a
summary here.

Documentation changes should be reachable from the docs hierarchy, use stable
relative links where possible, and avoid duplicating long procedures in several
places. Root can quote the rule; the owning doc should carry the examples,
procedure, and rationale.

## Common Pitfalls To Recheck

Profile leakage is easy to miss. Code that works for the default profile can
still be wrong when the user runs `hermes -p work`, a gateway profile, a cron
job, or a project-local workspace. Search for hard-coded home paths, direct
`Path.home()` use, ad hoc log paths, and config reads that bypass the profile
resolver. Tests should prove the active profile owns its config, sessions,
logs, memory, skills, and plugin state.

Provider fallback is not a generic retry loop. Keep auth failures, billing
errors, provider rate limits, upstream overload, and confirmed-empty credential
pools distinct. A change that treats every 429 or 401 the same can hide a
working fallback, hammer an exhausted account, or incorrectly consume another
credential. Preserve the existing error-classifier semantics unless the task is
explicitly to change them, and add negative controls for neighboring classes.

Import behavior can be load-bearing. Before adding `__init__.py`, moving a
module, widening package discovery, or changing plugin import order, check for
shadowing, namespace-package behavior, side effects at import time, and
registration timing. Plugin discovery should be cheap and robust; a broken
plugin must not prevent Hermes from starting.

Remote and subprocess environments differ from the local CLI. Gateway workers,
kanban workers, cron jobs, Desktop backends, TUI gateway subprocesses, Docker,
SSH, and remote terminal backends may have different current working
directories, homes, environment variables, PATHs, and mounted files. Validate
the actual execution environment when a change crosses that boundary.

Do not turn a test helper into the real contract. If production code and tests
need the same rule, extract a small runtime helper and test that helper through
the production caller. Tests that duplicate production logic can both pass
while the application remains broken.

## MacBook And Hermes Mac Mini

The Hermes Mac mini is the existing OpenSSH alias `mini`. Use `ssh mini` or
`ssh -o BatchMode=yes mini '<command>'` for non-interactive checks. The alias
and key selection are managed in `~/.ssh/config`; do not edit that managed
block by hand or create a duplicate Codex-only key.

The remote Hermes executable is `/Users/colingreig/.local/bin/hermes`. Codex's
Hermes connection reaches the same host through SSH. The mini keeps its own
service-account secret path. Do not copy or install the MacBook's 1Password
Connect setup on Hermes.

Treat mini operations as remote state. Verify the target host and paths before
changing services, launch agents, credentials, or configuration.
Governed mini-bundle work (`install_*.py` under `machine-setup/mini-scripts/`)
must be verified on `mini` via `ssh mini` before handoff — staged copy +
installer run, or `/ignite-ship`; do not defer with post-merge TODOs.
See `machine-setup/mini-scripts/README.md` for bundle-specific commands.

## Ignite And Board Work

Skills are operational procedures, not background context. When a user names a
skill, load and follow its `SKILL.md` before acting; do not substitute this
file for the live skill.

For named `/ignite-*` commands, invoke the matching Ignite skill and obey its
current references, including ClickUp, claim, handoff, validation, and shipping
rules. The skill owns details that must not be copied into this file.

`/ignite-execute` is the producer and `/ignite-validate` is the exclusive
completion gate. Executors hand work to review with the required proof packet;
they do not mark implementation tasks complete. Use `/ignite-ship` for
deployment rather than hand-rolling a platform deploy.

Content-bearing and hybrid Ignite work fails closed. Use the required blog or
content skill, run the configured/probed `content-qa/v1` path, certify the exact
changed paths, and provide the bound JSON report in the handoff. Never alter
vendored skills to make local policy fit; place local policy or workflow
customization outside the vendored skill.

When a task needs a capability not currently enabled, use the appropriate
skill's resolver or enablement procedure. Do not edit plugin caches or invent a
local workaround.

## Final Checklist

Before handing off repository work:

- Nearest scoped `AGENTS.md` read for touched surface.
- Required docs checked: `CONTRIBUTING.md`, `testing-conventions`,
  `plugins/index`, `adding-tools`, `agent-loop`, or `ui-tui/AGENTS.md` as
  applicable.
- Change is on the smallest viable Footprint Ladder rung.
- Prompt caching and message alternation are preserved.
- Config is in `config.yaml` unless it is a secret.
- Profile-scoped state uses `get_hermes_home()` and user-visible paths use
  `display_hermes_home()`.
- Tests are behavior/invariant tests, not source-shape or change-detector
  snapshots.
- Risky paths have real-path validation.
- New symbols are wired into production call sites.
- Remaining blockers or skipped checks are stated plainly.
