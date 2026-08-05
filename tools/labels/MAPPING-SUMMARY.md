# ClickUp Tag → Canonical Label Mapping — Summary

Source: live ClickUp tag inventory, 4 spaces (Delivery, Operations, Growth, CRM), pulled 2026-08-04.
Full mapping: `tools/labels/clickup-tag-map.json`. Read-only pull — no writes were made to ClickUp.

## Consolidation: 35 labels → 18 labels (2026-08-05)

Colin's call: the taxonomy was too big at 35 labels/6 namespaces. Cut to 18
labels/5 namespaces — less is more. What was cut, and why:

- **`src:` namespace deleted entirely** (`src:hermes`, `src:human`, `src:audit`)
  — provenance is already recorded by the issue author / task creator; a
  label for it is redundant.
- **`flow:needs-prep` removed** — the absence of `flow:prepped` already means
  the same thing.
- **`flow:prep-blocked` folded into `flow:blocked`** — one blocked signal,
  not two.
- **`flow:needs-validation` removed** — the board's "In Review" status
  already expresses it.
- **`flow:superseded`, `flow:duplicate` removed** — these are closure
  reasons; they belong in the close action/comment, not a label.
- **`lane:design` folded into `lane:content`**.
- **`model:human` folded into `flow:needs-human`** — the same signal
  expressed twice.
- **`area:pr-pipeline`, `area:validator` folded into `area:infra`.**
- **`area:billing` renamed `area:cost`.**
- **`area:data`, `area:reporting`, `area:seo`, `area:ux`, `area:content`
  removed outright** — no dedicated label; tags that mapped to them were
  re-targeted to a surviving label where one clearly fit, otherwise `delete`
  if the tag lived only in the Operations space or `keep_unmapped` if it
  lived in the Delivery (client-facing) space. In practice every affected
  tag had a live presence in Delivery, so all landed on `keep_unmapped` —
  client tags are never deleted.

`area:` remains the only *open* namespace, but the bar to add to it is now
explicit: a new value needs a PR and a stated reason the existing four
(`infra`, `security`, `observability`, `cost`) don't cover it.

The full list of retired label strings lives in `labels.json`'s
`rules.retired` block, so a future audit recognises them as deliberate kills
rather than new sprawl re-forming under an old name.

## Disposition counts (post-consolidation)

| disposition | count |
|---|---|
| rename | 42 |
| merge | 40 |
| explode | 10 |
| delete | 78 |
| keep_unmapped | 396 |
| needs_decision | 0 |
| **total** | **566** |

Total tags classified (566) matches total tags fetched (566) — Delivery 508, Operations 170, Growth 12, CRM 4, deduplicated to 566 distinct names across the union. `delete` grew from 60 to 78 (the 18 tags that pointed only at now-cut labels — `flow:needs-prep`, `flow:needs-validation`, `flow:superseded`, `flow:duplicate`, `src:*` — with no surviving replacement); `keep_unmapped` grew from 378 to 396 (the `area:data`/`reporting`/`seo`/`ux` tags that lost their target and had no surviving-area fit, but live in the Delivery space so were kept rather than deleted); `rename`/`merge` shrank correspondingly as their targets were re-pointed or removed.

## Tags per space, before / after

Applying the mapping (deletes removed, renames/merges/explodes collapsed onto their canonical targets, keep_unmapped/needs_decision left as-is):

| space | before | after |
|---|---|---|
| Delivery (90171361707) | 508 | 409 |
| Operations (90171361718) | 170 | 79 |
| Growth (90171361693) | 12 | 11 |
| CRM (90171348025) | 4 | 3 |

Operations shrinks the most (170 → 79) — it was carrying most of the epoch/id one-off automation artifacts (`attempt-cap-exceeded-*`, `blocked-operator-*`, `superseded-by-*`) and phase/rebuild codenames, all deleted, plus several bare taxonomy-shaped tags (`tier:1`, `blocked`, `agent-avoid`, etc.) that collapse via rename/merge. Delivery shrinks modestly (508 → 409) since the bulk of it is legitimate client/project/marketing vocabulary quarantined as `keep_unmapped`, not touched. CRM drops from 4 to 3 because its one `needs-validation` tag lost its target in the consolidation (`flow:needs-validation` was removed) and had no other surviving fit, so it became `delete`.

## needs_decision (0 tags — resolved)

Operator decision 2026-08-05: kill all 4. The following 4 tags, previously `needs_decision`, are now `delete`:

| tag | resolution |
|---|---|
| `aoe` | `delete` — operator decision 2026-08-05: kill |
| `aoe-dispatch` | `delete` — operator decision 2026-08-05: kill |
| `investigation-only` | `delete` — operator decision 2026-08-05: kill |
| `repo:ignite-marketplace` | `delete` — operator decision 2026-08-05: kill |

## Judgment-call notes (non-obvious defaults applied)

- `keep_unmapped` was applied to any legitimate non-junk tag lacking a clean taxonomy fit, not strictly limited to the Delivery space as the literal rule text suggests — several substantive Operations/Growth/CRM tags (`email-inbound`, `triaged`, `staged-no-ship`, `merged`, `presswhizz` in Operations, etc.) share the same "real signal, out of scope" profile and were quarantined the same way rather than deleted.
- `area:` was extended pragmatically beyond the example list where a tag unambiguously names a fleet subsystem in the original (v1, 35-label) pass (e.g. `area:billing` for `payment`/`gh-actions-cost`, `area:pr-pipeline` for `pr`/`hidden-pipeline`, `area:ux` for `ui`/`review-ux`). The v2 consolidation then folded or dropped those exact values per the table above — `payment`/`gh-actions-cost` now land on `area:cost`, `pr`/`hidden-pipeline`/`validation`/`validator` now land on `area:infra`, and `ui`/`review-ux` (no surviving `area:ux`) landed on `keep_unmapped`.
- Bare priority/color tags (`high`, `low`, `medium`, `high priority`, `low priority`, `medium priority`, `red`, `orange`, `yellow`, `green`) are all Delivery-only → `keep_unmapped`, per the stated rule; none of these appear Operations-only, so no deletes were needed there.
- `agent-ready`/`agent-avoid`/`agent-fenced`/`agent-review`/`agant-avoid` mapped per the routing-eligibility rule (`flow:prepped` / `flow:needs-human` / `flow:blocked` / — `agent-review` previously routed to `flow:needs-validation`, which is now removed, so it's `delete`); `agent-avoid` explicitly encodes "Hermes should not auto-claim it," not a lifecycle stage.
- Simple lexical/syntax variants of a single concept (`sonnet`, `model-sonnet`, `model:sonnet` all → `model:sonnet`; typos like `agant-avoid`) were treated as independent `rename` (in-place edit) operations even where multiple variants converge on the same canonical target, per the prompt's own examples. The explicitly-named `needs-colin`/`needs-operator`/`needs-human-review`/`needs human`/`validation-needs-human` group was kept as `merge` per the rule text.
