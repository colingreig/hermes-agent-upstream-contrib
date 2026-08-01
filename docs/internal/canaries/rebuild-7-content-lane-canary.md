---
title: REBUILD 7 content-lane canary
date: 2026-08-01
doc_type: canary
---

This canary proves the Hermes Mini `content-lane-executor` job reaches the governed ClickUp
review handoff through the approved direct path: invoking `/ignite-execute --lane content` and
producing a bounded, internal-only artifact.

The content profile is deliberately fail-closed. Its cron job contract pins `provider: anthropic`
and `model: claude-sonnet-5` with `no_fallback: true`, so a run either completes entirely on
Sonnet-5 or stops — it never silently substitutes a cheaper or different model to route around a
quota, rate limit, or provider outage. That guarantee matters most for content work, where a
substituted model can quietly shift voice, factual grounding, or editorial judgment without
tripping any obvious error.

This run produced zero fallback attempts, generated its handoff packet through the governed
tooling, and published no production content. The task reaches In Review purely on that
durable, criterion-by-criterion evidence trail.
