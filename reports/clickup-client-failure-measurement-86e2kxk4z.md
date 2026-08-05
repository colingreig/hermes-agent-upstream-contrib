# ClickUp client failure measurement — 86e2kxk4z

## Acceptance status: PENDING

The retained pre-deployment evidence recorded in the task thread covers 84 poll-gate cron runs over approximately 21 hours on 2026-08-03. It contains 0 terminal `clickup_poll_gate.py` HTTP/timeout markers. That is a run-level observation of 0 terminal markers among 84 retained runs, not a call-level baseline, and cannot demonstrate a positive reduction after the retry deployment. Older poll-gate output and per-call attempt counts were not retained. This document does **not** treat unrelated `clickup.mjs` 401 messages as poll-gate failures.

The instrumentation below enables prospective measurement only. It cannot reconstruct the missing historical zero-failure call-level baseline. The acceptance criterion requiring a deployed post-window that demonstrates material improvement therefore remains **pending** until comparable retained observations exist; this task must not be reported complete on telemetry implementation alone.

## Durable measurement added

The production-governed canonical and bundle-vendored copies of `scripts/clickup_poll_gate.py` append `clickup-client-call/v1` JSONL records to `~/.hermes/state/clickup-client-calls.jsonl`. Instrumentation covers both the gate's direct urllib calls and the principal queue-sync calls made by `clickup_sync.py` through subprocess curl, including failures that `load_team_task_index()` converts into stale-cache fallback. Each record includes a UTC timestamp, validated terminal outcome (`success`, `recovered`, or `failure`), attempt count, failure class, and elapsed milliseconds. Telemetry is fail-open and rotates at 10 MiB with five retained generations by default.

`scripts/clickup_failure_measure.py` filters to the expected client and valid outcomes, compares explicit half-open UTC windows only when they are ordered, non-overlapping, and equal-duration, and reports:

- retained logical-call count;
- terminal failure count and rate;
- retry-recovered call count;
- failure-class counts;
- relative failure-rate reduction and a configurable materiality threshold.

It exits nonzero rather than claiming improvement when either window has no retained calls or the baseline has zero terminal failures.

Example after two comparable retained windows exist:

```bash
python scripts/clickup_failure_measure.py \
  --before-start 2026-08-04T00:00:00Z \
  --before-end   2026-08-05T00:00:00Z \
  --after-start  2026-08-05T00:00:00Z \
  --after-end    2026-08-06T00:00:00Z
```

These future windows measure deployed behavior consistently; they do not retroactively manufacture a pre-retry historical baseline.
