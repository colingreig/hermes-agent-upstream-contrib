# ClickUp Tag → Canonical Label Mapping — Summary

Source: live ClickUp tag inventory, 4 spaces (Delivery, Operations, Growth, CRM), pulled 2026-08-04.
Full mapping: `tools/labels/clickup-tag-map.json`. Read-only pull — no writes were made to ClickUp.

## Disposition counts

| disposition | count |
|---|---|
| rename | 53 |
| merge | 65 |
| explode | 10 |
| delete | 60 |
| keep_unmapped | 378 |
| needs_decision | 0 |
| **total** | **566** |

Total tags classified (566) matches total tags fetched (566) — Delivery 508, Operations 170, Growth 12, CRM 4, deduplicated to 566 distinct names across the union.

## Tags per space, before / after

Applying the mapping (deletes removed, renames/merges/explodes collapsed onto their canonical targets, keep_unmapped/needs_decision left as-is):

| space | before | after |
|---|---|---|
| Delivery (90171361707) | 508 | 409 |
| Operations (90171361718) | 170 | 77 |
| Growth (90171361693) | 12 | 11 |
| CRM (90171348025) | 4 | 4 |

Operations shrinks the most (170 → 77) — it was carrying most of the epoch/id one-off automation artifacts (`attempt-cap-exceeded-*`, `blocked-operator-*`, `superseded-by-*`) and phase/rebuild codenames, all deleted, plus several bare taxonomy-shaped tags (`tier:1`, `blocked`, `agent-avoid`, etc.) that collapse via rename/merge. Delivery shrinks modestly (508 → 409) since the bulk of it is legitimate client/project/marketing vocabulary quarantined as `keep_unmapped`, not touched.

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
- `area:` was extended pragmatically beyond the example list where a tag unambiguously names a fleet subsystem (e.g. `area:billing` for `payment`/`gh-actions-cost`, `area:pr-pipeline` for `pr`/`hidden-pipeline`, `area:ux` for `ui`/`review-ux`).
- Bare priority/color tags (`high`, `low`, `medium`, `high priority`, `low priority`, `medium priority`, `red`, `orange`, `yellow`, `green`) are all Delivery-only → `keep_unmapped`, per the stated rule; none of these appear Operations-only, so no deletes were needed there.
- `agent-ready`/`agent-avoid`/`agent-fenced`/`agent-review`/`agant-avoid` mapped per the routing-eligibility rule (`flow:prepped` / `flow:needs-human` / `flow:blocked` / `flow:needs-validation` respectively); `agent-avoid` explicitly encodes "Hermes should not auto-claim it," not a lifecycle stage.
- Simple lexical/syntax variants of a single concept (`sonnet`, `model-sonnet`, `model:sonnet` all → `model:sonnet`; typos like `agant-avoid`) were treated as independent `rename` (in-place edit) operations even where multiple variants converge on the same canonical target, per the prompt's own examples. The explicitly-named `needs-colin`/`needs-operator`/`needs-human-review`/`needs human`/`validation-needs-human` group was kept as `merge` per the rule text.
