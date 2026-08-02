---
name: production-write-lease
description: Acquire, inspect, release, and evidence-recover Hermes Mini production-write leases when operating the fleet-config installer, Mini release cut, or another registered cross-repository production writer.
---

# Production write lease

Use the registry mapping exactly; never request a partial resource set. Cross-repo
partner ownership for ignite-email-infra poller deploy paths is documented in
`machine-setup/cross-repo-operating-contract.md` and
`machine-setup/ignite-email-infra.resource-manifest.json`. Hermes-side installers
fail closed on those paths unless this lease includes `purelymail-poller-deploy`
(which Hermes installers must never request — partner deploy PRs in
ignite-email-infra implement acquire/release in `poller/deploy-poller.sh`).

Inspect before any manual recovery:

```bash
hermes production-write-lease status
```

For a registered writer, acquire its complete resource mapping with a unique
session ID and exact 40-character commit SHA. Keep the JSON result: its lease
ID, fence, actor, and session ID are required for heartbeat and release.

```bash
hermes production-write-lease acquire \
  --actor mini-release-cut \
  --resources runtime-release governed-mini-scripts \
  --session-id "operator-$(date +%s)" \
  --workspace ~/.hermes --repo hermes-agent \
  --commit-sha <40-char-sha> --reason "governed cut"
```

Heartbeat immediately before each protected mutation and release only with the
same exact identity. If a lease is expired, do not reacquire around it: use
`recover` only after the original owner is known stopped and supply durable
JSON evidence. Recovery writes an inspectable receipt to the lease database.
