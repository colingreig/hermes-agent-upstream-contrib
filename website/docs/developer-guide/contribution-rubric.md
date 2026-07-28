---
sidebar_position: 1
title: "Contribution Rubric"
description: "What Hermes wants, what it rejects, and how to verify a contribution before reviewing or merging"
---

# Contribution Rubric

This is the project's intent layer. Use it two ways:

1. For contributors and maintainers: aim changes at what Hermes actually wants
   to merge.
2. For automated review and triage: recognize when a PR is safe to close for an
   allowed mechanical reason, and when a human maintainer should decide.

Hermes ships a lot. Bug fixes, new platforms, new providers, and product
surface work are welcome. The restraint in this rubric applies most strongly to
the core agent waist and the model tool schema, because every core tool and
prompt-shaping decision is paid for on every model request.

## What We Want

### Fix real bugs, well

The strongest contribution starts from a real symptom, reproduces it on current
`main`, identifies where the behavior manifests, and fixes the whole bug class.
Look for sibling call paths, async variants, gateway/CLI parity, profile
variants, and remote-backend versions of the same issue.

Do not stop at a plausible rationale. A confirmed reproduction and a line-level
account of where the fix acts beat a convincing story every time.

### Expand reach at the edges

New platform adapters, messaging channels, providers, models, Desktop/TUI
features, and dashboard support panels are welcome when they integrate through
the existing setup/config/plugin surfaces. Breadth is a product goal. The
question is how the capability is wired, not whether Hermes is allowed to grow.

Prefer setup flows like `hermes tools`, `hermes setup`, plugin installation,
provider profiles, and documented `config.yaml` settings over raw one-off
environment variables or special-case core branches.

### Refactor god-files into focused modules

Large, mechanical extractions from files like `cli.py`, `run_agent.py`, and
`gateway/run.py` are valid work when the request is the extraction. Keep the new
module boundary honest, preserve behavior, and validate the real path after the
move.

For feature PRs, every line should still trace to the requested behavior. For a
declared refactor, the request is the cleanup itself.

### Keep the core narrow

New model tools are the expensive exception because every model-tool schema is
sent on every API call. Prefer the Footprint Ladder:

1. Extend existing behavior.
2. Add a CLI command plus a skill.
3. Add a service-gated tool with `check_fn`.
4. Add a plugin.
5. Add an MCP server in the catalog.
6. Add a core tool only when the capability is fundamental and unreachable by
   terminal, file, skill, plugin, or MCP surfaces.

See [Adding Tools](./adding-tools.md), [Build a Hermes Plugin](./plugins/index.md),
and [Agent Loop Internals](./agent-loop.md) for the implementation homes.

### Extend, don't duplicate

Before adding a manager, hook, provider system, or module, check whether an
existing surface already covers it. When several PRs integrate the same category
of thing, design one shared interface and wrap the existing built-in as the
first provider rather than merging competing one-off integrations.

### Behavior contracts over snapshots

Tests should assert how pieces of data relate, not freeze today's catalog,
config version, or enum count. See [Testing Conventions](./testing-conventions.md)
for concrete examples of good invariant tests and rejected change-detector
tests.

### E2E validation for risky paths

Anything touching resolution chains, config propagation, security boundaries,
remote backends, persistent state, file I/O, network I/O, or tool dispatch needs
real-path validation with actual imports against an isolated `HERMES_HOME`.
Mocks are useful for narrow branches, but they do not prove the integration
works.

### Cache-, alternation-, and invariant-safe changes

Preserve prompt caching, strict message role alternation, and byte-stable system
prompt construction for the life of a conversation. New capabilities should not
rebuild the system prompt or swap the active toolset mid-session unless the
change is explicitly a context-compression or new-session boundary.

### Preserve contributor credit

When incorporating external work, salvage it where feasible through
cherry-pick/rebase-merge style flows so authorship survives in history. Do not
reimplement from scratch just because it is faster locally.

## What We Reject

### Speculative infrastructure

Hooks, callbacks, and extension points with no concrete consumer are rejected.
Adding a hook is cheap; removing one after plugins depend on it is expensive.

A hook is not speculative when a real contributor has a stated use case, even
if the consumer ships separately. In that case, design the smallest generic
surface that supports the concrete consumer.

### New non-secret `HERMES_*` user config

`.env` is for credentials only: API keys, tokens, passwords, and similar
secrets. Behavioral settings belong in `config.yaml`. If an implementation
needs an internal environment variable as a bridge, keep the user-facing docs
and setup flow pointed at `config.yaml`.

Reject PRs that tell users to set a non-secret behavior flag in `.env`.

### A new core tool when terminal, file, skill, plugin, or MCP works

If the only barrier is file visibility on a remote backend, fix the mount or
backend visibility. Do not add a core read variant. If the behavior can be
spelled as repeatable instructions and commands, make a skill. If it is niche
or user-specific, make a plugin or MCP server.

### Lazy-reading escape hatches on instructional tools

Do not add pagination like `offset`/`limit` to tools that load instructions the
agent must read fully, such as skills, prompts, or playbooks. Agents will often
read page one and skip the rest. Keep instructional reads complete.

### "Fixes" that destroy the feature they secure

A mitigation that disables the feature's purpose is the wrong mitigation. Read
the original intent before restricting behavior, then find a narrower fix that
keeps the feature useful.

### Outbound telemetry without opt-in gating

Do not add analytics, third-party identifier tagging, usage attribution, or
vendor telemetry until a generic user-facing opt-in exists, including config
gate, setup prompt, and a `hermes tools` or equivalent toggle.

### Plugins that touch core files for product-specific behavior

Plugins live in plugin directories and use generic plugin APIs. If a plugin
needs a broader surface, widen the generic plugin surface. Do not special-case a
specific plugin in the core tree.

### Third-party products integrated into the core tree

Observability backends, vendor SaaS connectors, analytics dashboards, and other
"someone else's product" integrations belong in standalone plugin repos. Users
install them into `~/.hermes/plugins/` or through a package entry point. This is
a coupling and maintenance policy, not a quality judgment.

## Verify the Premise Before Calling It a Bug

The most common reason a well-written PR gets closed is not code quality. It is
that the change is built on a wrong premise or treats intentional design as a
gap. These case studies are review guardrails.

### Case study: intentional design, not a gap

Profiles are independent islands on purpose. A change that adds live inheritance
from the default profile looks helpful, but it couples profiles in exactly the
way the design avoids. The legitimate "start from my default" workflow is a
copy-at-creation path such as `--clone`, not ongoing inheritance.

Before filling a missing link, ask whether the isolation is the design. Use
history and surrounding code to understand why the limitation exists.

### Case study: wrong mental model of existing behavior

A rate-limit PR proposed re-probing during cooldown. The premise was wrong: the
breaker only trips on a confirmed-empty account bucket, so re-probing simply
hammers a bucket already proven empty.

Another usage-accumulation fix added a branch that never executed because an
earlier guard already popped the state it depended on. The code looked
reasonable, but it changed no runtime behavior.

Trace the real call path before accepting the rationale. If you cannot point to
the line where the bug manifests and show how the fix changes that line's
behavior, you have not verified the premise.

### Case study: an omission was load-bearing

Restoring "missing" `__init__.py` files once made a test tree importable as a
dotted package that shadowed the real plugin and deleted its `register()` at
import time. The absence looked accidental. It was protecting import behavior.

When a fix adds the obvious missing file, import, config layer, or fallback,
check whether the omission prevents shadowing, cycles, cache invalidation, or
incorrect auto-discovery.

### Case study: resurrecting a rejected direction

Some PRs work technically but revive an approach the maintainers intentionally
moved away from, or supersede a narrower agreed base. Keep the change scoped to
the piece that was requested. Offer broader ideas as focused follow-ups instead
of bundling them into the current PR.

## Review Checklist

Before you approve, merge, or mechanically close a PR:

- Reproduce the symptom or verify the claimed gap against current `main`.
- Identify the runtime line or path where the fix acts.
- Check whether an apparent omission is intentional design.
- Place the capability on the smallest viable Footprint Ladder rung.
- Confirm config belongs in `config.yaml` unless it is a secret.
- Confirm tests are behavior/invariant tests, not source-shape or snapshot
  change detectors.
- Exercise the real integration path when the change crosses config, state,
  security, backend, file, network, or tool-dispatch boundaries.
- Preserve prompt caching and message alternation.
- Keep contributor authorship intact when adapting external work.

When in doubt, leave the PR open for human review rather than closing it as
`implemented_on_main`, `cannot_reproduce`, or `incoherent`.
