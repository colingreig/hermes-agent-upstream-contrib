# Gemini API usage investigation — 2026-07-12

## TL;DR

- The observed increase cannot yet be attributed to a workload, model, or API key: neither Gemini project has a billing export or request telemetry available.
- The two Google AI Studio projects are both billed to the same Ignite Marketing billing account; a new **Hermes v2 Gemini API Key** was created on 2026-07-07, at the start of the period in question.
- Hermes is a plausible future consumer, but the apparent local Gemini spike was test-fixture output, not evidence of production calls; the always-on `mini` host logged no Gemini attempts in its retained logs.
- The immediate priority is attribution and containment: give Hermes its own observable key/project, add a budget alert, and make its auxiliary-provider routing explicit.
- One needs-Colin action task should be created after this report is pushed.

## Scope and limits

This investigation covered 2026-07-05 through 2026-07-12 using the local Hermes state and logs, the `mini` Hermes host (read-only), source/configuration history, and read-only Google Cloud inventory.

It did **not** make production changes, enable Google APIs, create a billing export, rotate keys, or change routing. Exact billed tokens/costs are therefore unavailable: the two Gemini projects have neither an accessible detailed billing export nor Cloud Logging enabled. This is a material finding, not a reason to infer usage from client-initialization logs.

## Findings

### High — Gemini spend cannot be attributed to a caller, model, or key. **CONFIRMED**

Both `gen-lang-client-0050782291` (Gemini API) and `gen-lang-client-0271247397` (Hermes) have `generativelanguage.googleapis.com` enabled and are linked to the same active Ignite Marketing billing account. The read-only inventory found no billing-export dataset in the candidate BigQuery projects. A seven-day Cloud Logging query could not run because `logging.googleapis.com` is disabled on both Gemini projects.

**Impact:** a single billing-account increase cannot distinguish Hermes from the other keys/workloads, and no local evidence can turn it into a defensible attribution.

**Suggested action:** enable a Cloud Billing BigQuery export (with a deliberate retention/access decision), create a budget/alert for the Hermes project, and use a dedicated Hermes key/project. Report by project, SKU/model, and date before attributing the current increase.

**Needs Colin:** approve the billing-export dataset location, retention, and alert threshold; these change cloud configuration and may incur BigQuery storage/query cost.

### High — The new Hermes key is temporally correlated and insufficiently client-restricted, but is not proven to be the source. **UNVERIFIED**

`Hermes v2 Gemini API Key` was created in `gen-lang-client-0271247397` at `2026-07-07T02:52:49Z`. It is restricted to `generativelanguage.googleapis.com`, but the key metadata has no browser-referrer, server-IP, Android, or iOS application restriction. Its creation aligns with the start of the requested window, but Google has no per-key usage data enabled here, so correlation is not causation.

**Suggested action:** once export/alerting is in place, make this a Hermes-only credential, rotate it if its distribution is uncertain, and apply the strongest application restriction compatible with the actual runtime. Do not revoke it blindly before identifying dependent workloads.

### Medium — Hermes has a broad auxiliary routing surface that can select Gemini-family models. **CONFIRMED**

The documented default auxiliary path is `OpenRouter → Nous Portal → main endpoint`, and OpenRouter/Nous defaults are Gemini Flash ([`cli-config.yaml.example:457`](../cli-config.yaml.example#L457), [`cli-config.yaml.example:470`](../cli-config.yaml.example#L470), [`agent/auxiliary_client.py:609`](../agent/auxiliary_client.py#L609)). Hermes also supports a forced direct Google AI Studio provider ([`cli-config.yaml.example:474`](../cli-config.yaml.example#L474)) and assigns direct Gemini a Flash default ([`agent/auxiliary_client.py:417`](../agent/auxiliary_client.py#L417)).

This surface includes vision, web extraction, compression, session search, TTS audio tagging, titles, approval, MCP assistance, monitoring, triage, and profile description. The local `~/.hermes/config.yaml` currently contains only onboarding state, so it does not explicitly pin those tasks away from an available Gemini credential.

**Impact:** a valid direct Gemini credential can become an implicit fallback in provider-degradation scenarios, making spend unpredictable. Calls sent to OpenRouter/Nous should not be counted as direct Google AI Studio API spend, even when the selected model is Gemini.

**Suggested action:** explicitly configure auxiliary providers/models for each task class; until attribution is available, do not rely on `auto` for cost-sensitive work and keep direct Gemini limited to intentionally approved tasks.

### Low — The local Gemini-looking log burst is a test artifact, not production evidence. **CONFIRMED**

The local log retained only roughly 15 hours on 2026-07-11, not the whole week. Its Gemini entries included the exact mocked rate-limit text, model, and endpoint used by [`tests/agent/test_auxiliary_client.py:5301`](../tests/agent/test_auxiliary_client.py#L5301)–[`tests/agent/test_auxiliary_client.py:5316`](../tests/agent/test_auxiliary_client.py#L5316), along with other test-only provider placeholders. The `mini` host's retained logs contained no Gemini auxiliary attempts. The July 10 audit separately documented invalid Gemini credentials and an existing remediation task, so failed local attempts should not be treated as billable requests ([`reports/audit-hermes-setup-2026-07-10.md:43`](audit-hermes-setup-2026-07-10.md#L43)).

**Suggested action:** exclude test-process logs from any future usage analysis; record actual provider, model, response status, and provider-reported token usage in a local, opt-in cost ledger.

## What it is being used for

The only defensible answer today is **unknown at the billing level**. The candidate Hermes use cases are lightweight auxiliary work and, when routed through OpenRouter/Nous, Gemini-family inference paid to those intermediaries rather than directly to Google. No retained production Hermes or `mini` log establishes that the July 7 Hermes key served a billable request.

The billing increase may instead come from any of the older keys in the separate Gemini API project (including named client/scraper/image-generation keys), from the new Hermes key, or from another workload sharing the same billing account. The currently available telemetry cannot discriminate among them.

## Recommended sequence

1. **Today:** In Google Cloud Billing, filter the last seven days by the two Gemini projects and Gemini/Generative Language SKU. Compare daily cost and usage, then enable a BigQuery billing export and a Hermes-project budget alert.
2. **Before the next run:** assign Hermes a dedicated key/project or at least a dedicated key with a clear owner; verify the key's application restriction against its real server environment.
3. **In Hermes:** explicitly set auxiliary task providers/models. Keep `auto` only where its fallback cost is acceptable; use a named non-direct provider for routine compression/title/approval tasks.
4. **For durable diagnosis:** add opt-in local request/token/cost accounting, keyed by a non-secret credential fingerprint and provider/model/task. It must never log prompts or API keys and must remain outside the model prompt/tool surface.

## Dead ends

- Scheduled Hermes jobs are not the explanation: their saved `last_run_at`/`last_status` fields are empty.
- The always-on `mini` Hermes host has no retained direct-Gemini auxiliary events.
- The local rate-limit and invalid-key-looking sequence is covered by mocked auxiliary-client tests, so it is not evidence of paid Google traffic.
- Cloud Logging and the gcloud beta quota command were not enabled/installed; this audit did not change either.

## Method

| Dimension | Evidence | Result |
| --- | --- | --- |
| Local Hermes state | config, cron state, rotated logs, request-dump metadata | Tests/no scheduled execution; no week-long log coverage |
| Remote Hermes host | read-only `mini` log and cron inspection | No retained Gemini auxiliary attempts |
| Source and tests | routing defaults and fixture matching | Broad routing exposure; local burst disproved as production evidence |
| Google Cloud | projects, billing links, API-key metadata, service inventory | Two Gemini projects share billing; new Hermes key; no usable attribution pipeline |

This was a single bounded, read-only investigation. No subagents were used because the Codex runtime requires explicit user authorization for delegation.
