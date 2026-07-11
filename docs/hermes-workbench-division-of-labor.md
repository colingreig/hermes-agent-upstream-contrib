# Hermes ↔ ignite-workbench division of labor

> **DRAFT — pending Colin's approval.** This charter came out of the
> `ignite-audit` run on 2026-07-10 (§3a of
> `reports/audit-hermes-setup-2026-07-10.md`) and has not yet been ratified.
> Treat it as the proposed line, not the enforced one, until Colin signs off.

> **Living document.** Update this file whenever a new epic, a new system, or
> a system change shifts the boundary between the two platforms — this is
> meant to stay the current source of truth, not a point-in-time snapshot.

## Why this split exists

Colin runs **two** agent systems: **ignite-workbench** (a production
Next.js/Vercel agency-ops platform) and **Hermes** (an autonomous,
messaging-bridged agent fleet running on the mini). Both can, in principle,
touch ClickUp, write code, draft content, and send email — which means work
can land in the wrong place if nobody draws a line. This doc is that line: it
exists so the two systems stop duplicating each other's work and stop
reaching into each other's crown jewels. It is transcribed from the audit's
"3a. The charter" section and extended with the collision-zone clarifications
below.

## The charter

| | **ignite-workbench** — *"the factory"* | **Hermes** — *"the workshop crew + night watch"* |
|---|---|---|
| **What it is** | Production Next.js/Vercel agency-ops platform (client-facing). Deterministic Inngest pipelines grounded in real data. | Autonomous, messaging-bridged agent fleet on the mini working ClickUp boards. |
| **Owns** | SEO audits, PPC audits, monthly client reports, decks, ad-copy loop, AOE link-building, BigQuery ETL, Tarvec client portal, content publishing. | Code changes on repos, backlog execution/QA/validation, ingest/triage, monitoring, ad-hoc research + **content drafts**, personal-assistant messaging. |
| **Data grounding** | Google Ads / GSC / GA4 / Ahrefs / BigQuery / vendor APIs. | The repos + ClickUp + messaging. |
| **Human gate** | Client-facing artifacts approved before send. | PRs reviewed; `ignite-validate` QA gate. |

## Rule of thumb for "who does what"

- Needs client-data grounding + auditability + a client-approved artifact →
  **workbench**.
- Autonomous labor / maintenance / triage that produces an internal artifact
  or a PR → **Hermes**.
- **Neither should own the other's crown jewel.** Hermes should *not* run
  client-facing SEO/PPC pipelines (they belong in workbench's grounded,
  auditable Inngest jobs); workbench should *not* try to be the autonomous
  code-maintenance fleet.

## Collision zones to manage

Both systems touch these — keep them explicitly deconflicted rather than
letting ownership drift:

- **ClickUp writes** — keep board namespaces distinct between the two
  systems.
- **Fireflies** — watch shared rate limits; both systems can hit the same
  transcript API.
- **Autonomous email drafting** — shared review convention so a draft from
  one system isn't mistaken for (or silently overridden by) the other's.
- **Anthropic API keys** — separate keys per system is fine, but consolidate
  cost tracking if a single spend view is wanted.
- **Owned-vs-client publishing (explicit exception)** — autonomous
  draft-publish on **owned** sites (Ignite's own properties) is **Hermes'**
  lane; anything **client-facing** publishes through **workbench**. This
  reconciles the general "content publishing → workbench" line above with the
  narrower owned-site case addressed in sibling task
  [86e29q8ru](https://app.clickup.com/t/86e29q8ru).

## Related tasks

This charter was spun out of the 2026-07-10 audit's Division of Labor &
Growth Roadmap epic. Sibling tasks that refine or depend on this boundary:

- [86e29q8qz](https://app.clickup.com/t/86e29q8qz)
- [86e29q8ru](https://app.clickup.com/t/86e29q8ru)
- [86e29q8ta](https://app.clickup.com/t/86e29q8ta)
- [86e29q8tp](https://app.clickup.com/t/86e29q8tp)
- [86e29q8tu](https://app.clickup.com/t/86e29q8tu)

## Source

Transcribed from `reports/audit-hermes-setup-2026-07-10.md`, §3a ("The
charter (recommended)"), commit `4ac41321a`.
