# Decision: defer a second provider for the Mini research stage

**Date:** 2026-07-25  
**ClickUp task:** `86e2gdmfk`  
**Status:** Decision recorded; revisit when the triggers below fire  
**Scope:** `machine-setup/mini-scripts/research_exec.py`

## Decision

Do not add an automatic second search provider now.

Keep ScrapingBee as the single provider behind the existing bounded retry,
fail-open writer contract, and production monitor. If production evidence later
justifies a fallback, evaluate Tavily first because it exposes both search and
arbitrary-URL extraction, has a no-card free tier for a shadow trial, and
already has a tested Hermes provider implementation that demonstrates the
response shapes.

This is a decision to defer implementation, not to accept an unmonitored
single point of failure. The monitor thresholds below are the reopen signal.

## Evidence

### Current safeguards absorb the observed failure class

The research stage already:

- retries HTTP `429`, HTTP `5xx`, and transport failures up to three total
  attempts;
- uses bounded delays of one and three seconds and caps `Retry-After` at
  30 seconds;
- does not retry deterministic HTTP `4xx` responses;
- produces a deterministic degraded brief when search, fetch, key resolution,
  or analysis fails; and
- returns `writer_should_continue: true`, preserving the flag-and-ship
  contract instead of blocking delivery.

These behaviors are implemented by `REQUEST_RETRY_ATTEMPTS`,
`REQUEST_RETRY_BACKOFF_S`, `REQUEST_RETRY_AFTER_CAP_S`, `_request()`,
`deterministic_fallback_brief()`, and the fallback branches in
`machine-setup/mini-scripts/research_exec.py`.

The independent monitor also detects exhausted-retry search failures before
the normal 20-attempt sample floor:

- `search_failure_rate > 0.30` across at least three production attempts in
  the four-hour search window; or
- at least three consecutive most-recent production records with
  `search_failed=true`.

Either condition produces `status: "search-outage"` and exit code `2`.
`machine-setup/mini-scripts/research_stage_monitor.py` is the source of truth
for these thresholds.

### Production evidence is not yet sufficient to justify another live path

The Mini observation at `2026-07-25T19:33:55Z` was:

| Metric | Observed |
|---|---:|
| Recent receipts | 9 |
| Production enabled attempts | 1 |
| Smoke attempts | 6 |
| Served attempts | 1 |
| Search-window attempts | 0 |
| Search-failure streak | 0 |
| Status | `insufficient-data` |

The stage has therefore not observed a sustained production search outage.
Its one production attempt is below the monitor's 20-attempt confidence floor,
and there are no search failures in the current short window.

ScrapingBee's public status page reported 99.873% Fast Search API uptime over
its displayed 90-day window when this decision was recorded. Its July 24
notice described an outage of several dedicated APIs while stating that Fast
Search was working. That vendor status does not prove the task's individual
request succeeded, but it is additional evidence against treating one
transient request incident as a demonstrated sustained Fast Search outage.

### A fallback adds a second production contract

A correct fallback is more than a second endpoint. It needs:

- another credential resolved through the stage's in-memory secret path;
- search and fetch response normalization into the existing bounded,
  content-free receipt schema;
- the same untrusted-data and maximum-response-size controls;
- deterministic provider selection and failure attribution;
- tests for primary failure, fallback success, dual failure, rate limits,
  partial extraction, and secret redaction; and
- monitoring that distinguishes primary degradation from fallback masking.

The existing `plugins/web/tavily/provider.py` reduces uncertainty about Tavily
response normalization, but it is not directly interchangeable with this
standalone Mini stage. The plugin reads an environment variable and participates
in Hermes's general web-tool provider system, while the research stage resolves
its credential lazily and deliberately remains separate from the tool-capable
agent runtime.

Adding that second path before there is production outage evidence would
increase code, credential, billing, and monitoring surface without a measured
delivery or grounding benefit. The writer already continues under a primary
outage, so the residual risk is lower-quality grounding rather than a blocked
content pipeline.

## Provider comparison

| Provider | Relevant capability | Integration consequence | Decision |
|---|---|---|---|
| Tavily | Separate `/search` and `/extract` endpoints; extract accepts one or more arbitrary URLs. The free tier currently provides 1,000 credits/month without a credit card; pay-as-you-go is available. | Closest match to the stage's search-then-fetch shape. Existing Hermes normalization code and tests can inform a dedicated, secret-safe adapter. | Preferred contingency and first shadow-trial candidate. |
| Firecrawl | `/v2/search` can optionally scrape search results and return page content. The current free tier provides 1,000 credits/month; paid plans add higher limits and concurrency. | Capable, but introduces a broader search/scrape API contract and its own credit/concurrency semantics. It offers no current evidence-based advantage over Tavily for this bounded stage. | Retain as an alternative only if Tavily fails the shadow comparison. |
| Brave Search | Web Search returns ranked results and snippets. Brave's LLM Context endpoint returns query-selected, pre-extracted content. | The ordinary search endpoint does not replace arbitrary-URL extraction, while LLM Context would change the current search-then-fetch semantics rather than act as a drop-in fallback. | Not the first contingency for this stage. |

## Reopen and activation triggers

Reopen this decision when any one of these conditions is observed:

1. The production monitor returns `status: "search-outage"` after the built-in
   retry policy is exhausted.
2. Two provider-attributed `search-outage` incidents occur within 30 days,
   even if each recovers before a manual investigation.
3. A provider-attributed outage materially reduces grounding, observed as
   `status: "ungrounded"` or `status: "fetch-degraded"`, rather than merely
   producing a transient request error.
4. A product requirement changes the contract so research availability must
   block delivery; the present fail-open risk calculation would then no longer
   apply.

Trigger 1 starts an investigation, not an automatic vendor switch. Before
activating Tavily:

1. run it in shadow mode with no writer-routing effect;
2. collect at least 20 non-smoke attempts, matching the existing monitor's
   minimum sample floor;
3. compare grounded pages, attempted fetches, latency, and per-run cost against
   the same queries on the primary path;
4. verify that no query, fetched content, generated brief, or credential enters
   the receipt ledger; and
5. enable fallback only if the shadow results meet the existing grounding and
   safety contracts.

## Reproducible verification

Run from the repository root:

```bash
rg -n \
  'REQUEST_RETRY_ATTEMPTS|REQUEST_RETRY_BACKOFF_S|REQUEST_RETRY_AFTER_CAP_S|writer_should_continue' \
  machine-setup/mini-scripts/research_exec.py

rg -n \
  'DEFAULT_MIN_ATTEMPTS|DEFAULT_SEARCH_WINDOW_HOURS|MIN_SEARCH_WINDOW_ATTEMPTS|SEARCH_FAILURE_RATE_ALARM|SEARCH_FAILURE_STREAK_ALARM' \
  machine-setup/mini-scripts/research_stage_monitor.py

.venv/bin/python -m pytest -q \
  machine-setup/mini-scripts/tests/test_research_stage.py \
  machine-setup/mini-scripts/tests/test_research_stage_monitor.py
```

Observe the current production ledger without reading credentials or content:

```bash
ssh mini 'python3 ~/.hermes/scripts/research_stage_monitor.py'
```

The output should be treated literally. `insufficient-data` is not healthy,
but it also is not evidence of an outage. `search-outage` is the explicit
reopen signal.

Verify the contingency's existing Hermes capability:

```bash
rg -n \
  'supports_search|supports_extract|def search|def extract' \
  plugins/web/tavily/provider.py
```

## External sources checked

All provider capability and price observations above are snapshots and should
be rechecked when this decision is reopened:

- Tavily API endpoints:
  <https://docs.tavily.com/documentation/api-reference/introduction>
- Tavily arbitrary-URL extraction:
  <https://docs.tavily.com/documentation/api-reference/endpoint/extract>
- Tavily credits and pricing:
  <https://docs.tavily.com/documentation/api-credits>
- Firecrawl search with optional scrape:
  <https://docs.firecrawl.dev/api-reference/endpoint/search>
- Firecrawl pricing:
  <https://www.firecrawl.dev/pricing>
- Brave Web Search:
  <https://api-dashboard.search.brave.com/api-reference/web/search/get>
- Brave LLM Context:
  <https://api-dashboard.search.brave.com/documentation/services/llm-context>
- ScrapingBee service history:
  <https://status.scrapingbee.com/>

## Outcome

The recommended outcome is **not worth the implementation complexity at
current evidence**. Retry is sufficient for transient failures, the writer
remains available under degradation, and the monitor supplies an objective
signal to reopen the decision. Tavily is the documented contingency if that
signal fires and a bounded shadow trial confirms comparable grounding and
safety.
