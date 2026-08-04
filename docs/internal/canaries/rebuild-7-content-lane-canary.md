---
title: REBUILD 7 content-lane canary
date: 2026-08-03
doc_type: canary
---

This canary verifies the Hermes Mini content lane's fail-closed model routing after the
dedicated content-lane-executor job was operator-retired in favor of the unified
clickup-executor job. The unified job's own model/provider fields describe its primary
coder tier (openai-codex/gpt-5.6-sol), so a content-lane draw could no longer inherit
correctness by construction the way the dedicated job once did.

PR #319 closed that gap: `_apply_weighted_lane_to_job` in `cron/scheduler.py` now pins
`provider=anthropic` and `model=claude-sonnet-5` whenever the selected lane is
`content`, in addition to the existing `no_fallback=true`. Code-lane dispatch is
unchanged. This run — session `cron_62714b869845_20260803_170030` — is the first live
content-lane draw of the unified job since that fix merged and deployed, and the agent
turn log confirms every API call in this session used `model=claude-sonnet-5
provider=anthropic`, with zero fallback attempts observed.
