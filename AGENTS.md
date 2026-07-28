# Hermes Agent — Operating Contract

## Purpose and delivery

Hermes is one personal AI-agent core surfaced through the CLI, messaging
gateway, TUI, dashboard, and Electron desktop app. It learns across sessions,
can delegate work, and is extended primarily through skills and plugins.

Unless the user says otherwise, finish repository work: implement it, run the
relevant validation, commit, push, and open the normal review/PR handoff. Do
not silently leave uncommitted changes, skipped checks, or an unreported
blocker. Preserve unrelated work already present in a checkout.

Read the nearest scoped `AGENTS.md` and the relevant documentation before
changing a specialized surface. The filesystem is authoritative; do not rely
on stale file counts, code tours, or recalled paths.

## Core invariants

### Prompt-cache stability

A long-lived conversation reuses a cached prompt prefix. Do not mutate its
past context, tool set, memories, or system prompt mid-conversation; context
compression is the sole exception. New slash commands or settings that affect
prompt state should take effect next session by default, with an explicit
immediate-invalidation option only when necessary.

Keep message-role alternation valid. Do not inject synthetic user messages
mid-loop or create adjacent messages with the same role.

### Narrow core, capable edges

Every core model-tool schema is sent on every model request. Prefer the least
permanent surface that solves the problem:

1. Extend existing behavior.
2. Add a CLI command plus skill.
3. Add a service-gated tool when structured I/O is essential.
4. Add a plugin or MCP server.
5. Add a core tool only when it is broadly fundamental and cannot be reached
   through existing tools.

Do not add speculative hooks or frameworks. A plugin must use a generic,
existing extension seam; do not hard-code plugin-specific behavior into core.
New integrations for third-party products belong in standalone plugins, not
new in-tree product directories. For an intentional core tool, use the tool
registry and explicitly expose it through the appropriate toolset; discovery
alone does not make it available to agents.

Favor behavior/invariant tests over snapshots of model lists, version numbers,
or enum counts. Changes across configuration, resolution, security, remote
backends, or file/network I/O need a real-path test with actual imports, not
only mocks.

## Repository map and surface boundaries

Start from the relevant implementation rather than broad rewrites:

- `run_agent.py`, `model_tools.py`, and `toolsets.py` form the agent/tool
  waist. Tool modules register through `tools/registry.py`.
- `cli.py` and `hermes_cli/` own CLI orchestration, configuration, setup,
  plugins, and command registration.
- `gateway/` owns messaging platforms; `tui_gateway/` owns the Python
  JSON-RPC backend.
- `ui-tui/` is the Ink terminal UI. Its README contains its development and
  verification commands.
- `apps/desktop/` is a separate Electron + React chat surface. Its own
  `apps/desktop/AGENTS.md` and `DESIGN.md` are mandatory for Desktop work.

The dashboard chat embeds the real `hermes --tui` through a PTY. Do not build
a second React transcript or composer for the dashboard; extend Ink instead.
Desktop is different: it has its own renderer and talks to the headless Hermes
gateway over JSON-RPC. It does not embed the dashboard or TUI. Keep renderer,
Electron, and backend authority boundaries intact as described in its scoped
instructions.

For slash-command work, use the central command registry and trace every
affected surface (CLI, gateway, TUI, Desktop) rather than adding an isolated
handler. Desktop's curated palette must continue to surface user extensions
(skills and quick commands), not only its built-in allow-list.

## Profiles, state, and configuration

Hermes supports isolated profiles. Use `get_hermes_home()` for persistent
state and `display_hermes_home()` in user-visible paths; do not hard-code
`~/.hermes` or `Path.home() / ".hermes"` in profile-scoped code. Tests must
keep state in their temporary `HERMES_HOME`, and profile tests also need to
control `Path.home()` where profile-root behavior is under test.

User behavior belongs in `config.yaml`; `.env` is for credentials and other
secrets only. Do not introduce a user-facing `HERMES_*` environment variable
for non-secret configuration. Put user state under the active Hermes home and
keep schemas/descriptions profile-aware.

## Development and verification

Use the repository test wrapper, not direct `pytest`:

```bash
scripts/run_tests.sh                                  # full Python suite
scripts/run_tests.sh tests/gateway/                   # focused directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # focused test
```

It selects `.venv`, then `venv`, then the shared Hermes venv and runs tests in
a hermetic environment. Activate `.venv/bin/activate` (or `venv/bin/activate`)
for other Python development commands when needed.

For TypeScript packages, run the package's documented command from that
package (for example `ui-tui` or `apps/desktop`); do not assume a Python test
run validates a frontend change. For Desktop, follow its scoped checks. Run
the smallest relevant test first, then broader validation proportionate to
risk. Report any check that cannot run and why.

When adding a dependency, use bounded versions; pin Git sources and GitHub
Actions to immutable commit SHAs. Do not weaken security, credential, or
approval behavior merely to make a test pass.

## MacBook and Hermes Mac mini

The Mac mini is the existing OpenSSH alias `mini`. Use `ssh mini` (or
`ssh -o BatchMode=yes mini '<command>'` for non-interactive checks); its key
selection and alias configuration are managed already. Do not edit that
managed SSH block or create a Codex-only duplicate key.

The remote Hermes executable is `/Users/colingreig/.local/bin/hermes`. Codex's
Hermes connection reaches the same host over SSH. The mini has its own
service-account secret path: never copy or install this MacBook's 1Password
Connect setup on it. Treat mini operations as remote state; verify the target
host before changing services or configuration.

## Skills, plugins, and Ignite work

Skills are operational procedures, not background context. When a user names
a skill, load and follow its `SKILL.md` before acting; do not substitute a
handwritten approximation. For a named `/ignite-*` command, invoke the
matching Ignite skill and obey its current references, including its ClickUp,
claim, handoff, validation, and shipping rules. The skill owns details that
must not be copied into this file.

In particular, `/ignite-execute` is the producer and `/ignite-validate` is the
exclusive completion gate. Executors hand work to review with the required
proof packet; they do not mark implementation tasks complete. Use
`/ignite-ship` for deployment rather than hand-rolling a platform deploy.

For blog or other governed content, invoke the codex-blog `/blog` skill
(normally with the `/ignite-blog` overlay when working through Ignite) instead
of drafting or auditing content ad hoc. Resolve or enable that capability by
its supported procedure; if it is unavailable, do not bypass it. Use a stable
blog engine at least v2.0.0 and follow its `content-qa/v1` contract.
Content-bearing and hybrid work fails closed: run the configured/probed QA
path, certify the exact changed paths, and provide its bound JSON report in
the review handoff. Never alter the vendored blog skill; place local policy or
workflow customization outside it.

The global skill/plugin installation is deliberately lean; repository-specific
plugins are additive. When a task needs a capability not currently enabled,
use the appropriate skill's enablement/resolver procedure instead of editing
plugin caches or inventing a local workaround.

## Change hygiene

Make the smallest coherent change that satisfies the request. Read the code
path and its callers before treating a behavior as a bug; some absences and
restrictions are intentional. Preserve contributor authorship when integrating
existing work. Do not add telemetry, attribution, or third-party outbound
tracking without an explicit user-facing opt-in.

Before delivery, check that the implementation is scoped, profile-safe,
cache-safe, and tested on the affected surface. Summarize the change, tests,
and any remaining limitation in the handoff.
