# prod-live-patches promotion

`.github/workflows/sync-prod-live-patches.yml` is the sole automated authority
for advancing `prod-live-patches` from `main`. It starts after the `CI`
workflow completes on `main`; daily and manual recovery runs execute the same
certificate path and cannot bypass it.

The workflow certifies all of the following before it can push:

- the candidate is the exact current `refs/heads/main` object ID;
- the latest exact-head `CI` run is a `push` run on `main`, is completed, and
  concluded `success`;
- that run's `All required checks pass` aggregate job is completed and
  concluded `success`;
- the structured repository freeze variable is present, audited, and unfrozen;
- `prod-live-patches` is an ancestor of the certified SHA.

Missing, pending, skipped, cancelled, failed, wrong-SHA, ancestor-only, stale,
or ambiguous evidence fails closed. Immediately before pushing, the workflow
re-fetches both refs, asserts that `origin/main` still equals the certified SHA,
asserts that the deploy branch still equals the previously observed SHA, and
uses `--force-with-lease` as the branch compare-and-swap.

## Freeze variable

Create the repository variable `PROD_LIVE_PATCHES_FREEZE` with a JSON value.
Both frozen and unfrozen states require the actor, reason, and change time so
the certificate carries an auditable decision:

```json
{
  "schema": "prod_live_patches_freeze/v1",
  "frozen": false,
  "actor": "release-owner@example.com",
  "reason": "normal governed promotion",
  "changed_at": "2026-07-29T12:00:00Z"
}
```

To freeze, set `frozen` to `true` and update all audit fields. A missing,
malformed, unaudited, or frozen value blocks certification. Changing the
variable does not mutate branches or retry a workflow; use the normal manual
dispatch when a governed recovery run is required.

## Certificates and receipts

`scripts/certify_prod_live_patches.py` is deterministic and has no network or
git operations. The workflow supplies read-only GitHub API evidence. A
successful advancement is verified against the remote ref and then emits:

```text
promotion-receipt-<sha256-of-receipt-bytes>.json
```

The immutable artifact name contains the same receipt ID. Its payload records
the workflow authority and run, exact promoted head, prior deploy-branch SHA,
CI run and aggregate job identities, certificate ID, and complete freeze state.
No receipt is emitted for a no-op where the deploy branch already equals the
certified SHA.
