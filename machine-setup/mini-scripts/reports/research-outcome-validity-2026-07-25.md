# Research-stage outcome validity report

Generated: 2026-07-25T19:49:18Z from the Hermes Mini production ledgers.

## Result

**Insufficient data.** Research-stage degradation cannot yet be evaluated as a
predictor of content quality. The production research ledger contained one
non-smoke task ID, but no content-outcome receipts existed before this
instrumentation landed, so zero rows could be joined to citation coverage or a
validator verdict.

This is the required baseline finding, not a claim that degradation has no
effect. The old ledger also predates the current `grounded_pages` and `severity`
fields for these runs, so retroactively assigning cohorts would invent evidence.

## Observed coverage

| Signal | Count |
| --- | ---: |
| Production research task IDs | 1 |
| Content-outcome task IDs | 0 |
| Joined by ClickUp task ID | 0 |
| Joined with known research severity | 0 |
| Joined with validator verdict | 0 |

## Instrumentation now in place

- Every successful `opencode_exec.py --content` run records a content-free
  receipt in `~/.hermes/logs/content-outcomes.jsonl`.
- A receipt contains the content-piece count, unique explicit external
  Markdown/HTML citation-link count, mean citation links per piece, the share
  of pieces containing at least one citation link, and a hash of the measured
  path set. It never records prose, URLs, or filenames.
- `research_outcome_metrics.py report` joins the latest receipt for each
  ClickUp task ID to `research-served.jsonl` (`grounded_pages`, `severity`) and
  `.validator_verdicts.json` (`PASS`, `BLOCK`/`FAIL`).
- `validator_fail_rate_for_content` is `BLOCK`/`FAIL` divided by all known
  validator outcomes among joined content tasks.
- `citation_link_coverage_per_piece` is unique explicit external citation links
  divided by measured content pieces.

## Evidence gate

The report remains `insufficient-data` until both the degraded
(`material`/`partial`) and healthy (`none`) cohorts contain at least five tasks
with validator verdicts. Once that floor is met it reports the observed
validator-fail-rate and citation-links-per-piece deltas. The result is labelled
as an association, never causation.

Reproduce on Hermes Mini:

```bash
python3 ~/.hermes/scripts/research_outcome_metrics.py report \
  --format markdown
```
