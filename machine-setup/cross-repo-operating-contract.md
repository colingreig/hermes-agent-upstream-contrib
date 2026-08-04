# Cross-repository operating contract — hermes-agent ↔ ignite-email-infra

**Status:** authoritative for Hermes-side ownership and guards. Partner
implementation PRs land in `ignite-email-infra`; this repository ships the
contract, resource manifest, and Hermes-side CI/runtime guards only.

**Tracking:** ClickUp `86e2kmud6` · registry predecessor `86e2kmucq` (validator-PASS).

## Purpose

Conductor workspaces on `hermes-agent` and `ignite-email-infra` may develop
in parallel, but the Hermes Mac mini has **exactly one production owner per
shared resource**. This contract names those owners, the lease boundaries,
and the recovery path when a cross-repo deploy races a Hermes installer.

Canonical machine inventory: `machine-setup/production_mutation_registry.json`.
Partner path manifest: `machine-setup/ignite-email-infra.resource-manifest.json`.

## One owner per shared resource

| Registry resource | Mini paths (summary) | Production owner | Hermes may |
| --- | --- | --- | --- |
| `purelymail-poller-deploy` | `~/.hermes/deploy/purelymail-poller/**`, runtime lock | `ignite-email-infra-poller-deploy` (`poller/deploy-poller.sh`) | Read/schedule cron; **never** stage or activate poller bytes |
| `purelymail-poller-deploy` (runtime) | `~/.hermes/scripts/purelymail-notify-poller.py` + companion artifacts | same | Cron may **execute** hash-verified live script only |
| `cron-jobs` (schedule only) | Hermes cron job `6e25865a22a4` | `fleet-config-installer` for schedule registration | Register cadence; script bytes remain ignite-email-infra owned |

Hermes actors (`fleet-config-installer`, `mini-release-cut`, skill sync, executor
cron jobs) **must not** write under `~/.hermes/deploy/purelymail-poller/` or
replace live poller artifacts. Unknown paths fail closed per the production
mutation registry.

## Conductor workspace rules

1. **Target repo is binding.** Tasks whose Execution Brief says
   `Target repo: hermes-agent` stay in this checkout; poller deploy code changes
   belong in `ignite-email-infra` and ship through that repo's PR + deploy path.
2. **Develop freely, mutate exclusively.** Local edits, tests, and docs may
   reference partner paths, but production mutation requires the registered actor
   and its lease/lock boundary.
3. **No skill-path workaround.** Do not copy Ignite skills into `~/.hermes/skills`
   to bypass `IGNITE_SKILLS_ROOT`. The overlay mirror via
   `machine-setup/mini-scripts/ignite-skills-pull.sh` is ops convenience only;
   canonical skill authority remains the ignite-skills checkout.
4. **Shared lease, distinct actors.** Both repos use
   `~/.hermes/state/production-write-lease.db`, but each actor must acquire the
   **complete** resource mapping from the registry. Partial mappings are rejected.

## Purelymail poller schedule authority

| Field | Value |
| --- | --- |
| Hermes cron job id | `6e25865a22a4` |
| Job name | Purelymail notify-me poller |
| Live cadence (2026-08-03, reconciled) | `*/15 * * * *` |
| Retired job id (must stay absent) | `6139465f559f` |

**SLA tradeoff:** at `*/15`, three consecutive scheduler misses imply ~45 minutes
before a skip is treated as an outage signal. A `*/45` cadence was briefly live
2026-08-01 through 2026-08-03 (scheduler drift, not an intended change) and
implied ~135 minutes for the same three-miss window; reconciled back to `*/15`
2026-08-03. Schedule changes are Hermes-side
(`machine-setup/fleet-config/jobs.json` via the fleet installer) but must not
change poller script bytes — those remain ignite-email-infra owned.

## Shared production-write lease — deploy-poller.sh call sites

Hermes documents the partner contract here; **implementation** of shared-lease
acquire/release inside `poller/deploy-poller.sh` is tracked as a separate
ignite-email-infra task/PR.

| Phase | Partner entry point | Lock / lease surface |
| --- | --- | --- |
| Acquire | `poller/deploy-poller.sh deploy --yes` remote activation shell | `fcntl LOCK_EX` on `~/.hermes/state/purelymail-poller/.lock` (today); wrap with `hermes production-write-lease acquire --actor ignite-email-infra-poller-deploy --resources purelymail-poller-deploy` in the partner PR |
| Stage | same shell, before atomic renames | writes only under `~/.hermes/deploy/purelymail-poller/incoming/<release_id>/` |
| Activate | atomic renames into `~/.hermes/scripts/` | holds runtime lock across the rename transaction |
| Release | successful activation or `rollback --yes` | lock fd closed; lease released with matching fence token |

Hermes-side governed entry points (`install_fleet_config.py`, `mini-release-cut.sh`)
call `machine-setup/cross_repo_poller_guard.py` so any accidental destination under
the poller deploy tree fails unless the active lease includes
`purelymail-poller-deploy`.

## Containment, recovery, and escalation

**Containment**

- If a Hermes installer or release cut fails closed on a poller path, stop —
  do not override the guard. No `--force` bypass exists on the Hermes side.
- If deploy-poller reports exit 75 (`poller runtime is active`), wait for the
  current cron fire to finish or investigate a stuck lock before retrying.

**Recovery**

- Poller rollback: `ignite-email-infra/poller/deploy-poller.sh rollback --yes`
  (selects the latest verified backup under `~/.hermes/deploy/purelymail-poller/backups/`).
- Hermes fleet recovery: normal installer/release-cut receipts under
  `~/.hermes/logs/fleet-config-installs/` and `~/.hermes/releases/.mini-release-last-receipt.json`.
- Never hand-edit live poller bytes under `~/.hermes/scripts/`; restore through
  the partner deploy procedure.

**Escalation**

- Cross-repo race or unknown writer: inspect `hermes production-write-lease status`
  and the production mutation registry actor list before any manual mutation.
- Genuine external/vendor/credential gates → ClickUp Activation list (`Needs Human`),
  not silent retries from Conductor workspaces.

## Related Hermes delivery surfaces

| Surface | Pointer |
| --- | --- |
| Production writer index | `machine-setup/fleet-config/PRODUCTION_WRITERS.md` (generated from registry) |
| Fleet installer lease | `machine-setup/fleet-config/README.md` § Production-write lease |
| Mini release-cut lease | `scripts/MINI-RELEASE.md` |
| Operator skill | `skills/production-write-lease/SKILL.md` |
| Manual deploy exemption | `docs/deploy/hermes-agent-ignite-ship-exemption.md` |
| Partner deploy entry | `ignite-email-infra: poller/deploy-poller.sh` |

Implementation PRs for poller deploy lease integration and script changes **must
not** land in `hermes-agent` except for contract/guard updates like this document.
