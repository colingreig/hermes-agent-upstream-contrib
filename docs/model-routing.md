# Model routing policy — content-creation jobs

**Status:** authoritative policy for model selection on content-creation cron
jobs. Applies to `hermes-agent` job configuration (`jobs.json`, per-job
`model`/`no_fallback`) and to the opencode-lane cascade referenced below.

Content-creation jobs must not silently substitute a cheaper or different
model on failure — a degraded model producing client-facing content is worse
than no output. The policy is deliberately fail-closed:

> Content-creation jobs run anthropic/claude-sonnet-5 only. If Sonnet cannot be served (spend cap, rate limit, credential failure), the run fails closed — skip + alert. No fallback, no rampdown, no substitute model, under any FailoverReason. Enforced by per-job no_fallback:true (hermes-agent) and a single-tier CONTENT_CASCADE (opencode lane, 86e29zvhh).

See `docs/chronos-managed-cron-contract.md` for how the desired job state in
`jobs.json` is reconciled and fired.
