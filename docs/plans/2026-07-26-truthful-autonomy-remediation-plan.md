# Truthful Autonomy Remediation Plan

## Purpose

The July 26 investigation found a recurring operational failure class: a scheduler, controller, or health surface could report success while child work failed, execution rows remained `running`, or the deployed Mini release did not match the source-controlled implementation. This plan restores a truthful execution contract, repairs the current red jobs, reconciles proven-dead historical rows, and makes live Mini health attestation authoritative.

The existing ClickUp epic is the program record: [Truthful health + loop integrity remediation](https://app.clickup.com/t/86e2gmfym). Do not create a replacement epic.

## Principles

- Preserve prompt caching and keep the core narrow. This work belongs in existing cron, ledger, deployment, and verifier surfaces rather than new core model tools.
- Machine failures win over assistant prose. A failed preflight, child process, required tool, malformed result, or timeout must be red everywhere it is recorded.
- Use source-controlled configuration for behavior. Secrets remain secret-only configuration.
- Preserve validator-only terminal completion. Executors ship and hand off at `in review`; only `ignite-validate:` proof may mark work complete.
- Encode sequencing with native ClickUp dependencies and `prep-blocked`, not prose alone.
- Keep migration/reconciliation reviewable, idempotent, and non-destructive. Never delete historical rows or replay side effects automatically.

## ClickUp Work Graph

All new work is a child of epic `86e2gmfym`, starts in `to do`, has `prepped`, `lane:code`, and exactly one colon-form `model:*` tag. No new child starts `agent-ready`.

1. [Guarantee terminal execution state with leases and reconciliation](https://app.clickup.com/t/86e2gnq7x) (`model:opus`)
   - Add ownership tokens, leases/heartbeats, terminal reasons/timestamps, schema migration, and compare-and-swap idempotent finalization to the execution ledger.
   - Add a dry-run stale-record classifier that produces a reviewable manifest and does not mutate historical rows.
   - Prove crash, signal, timeout, restart, PID reuse, live-gateway child death, and concurrent-finalizer behavior.

2. [Make cron outcomes reflect child and required-tool failures](https://app.clickup.com/t/86e2gnq8p) (`model:opus`, blocked on task 1)
   - Define one normalized internal outcome propagated to controller result, child process exit, execution row, and job last-status.
   - Missing credentials, skills, workdirs, malformed results, required-tool failures, and timeouts are red.
   - Assistant prose cannot override a machine failure.

3. [Repair the currently red scheduled jobs](https://app.clickup.com/t/86e2gnq8x) (`model:sonnet`, blocked on task 2 and cron environment repair)
   - Reproduce and repair Spam-gate label accrual exit 1.
   - Preserve research-monitor `insufficient-data` as an observable pre-threshold advisory outcome; threshold breach must fail and alert.
   - Require one forced run plus two healthy scheduled cycles for each repaired job.

4. [Reconcile historical false-running executions](https://app.clickup.com/t/86e2gnq90) (`model:sonnet`, blocked on tasks 1-3, environment repairs, and release convergence)
   - Snapshot the ledger and emit a reviewable dry-run manifest.
   - Transition only provably dead rows to `interrupted` or `failed` with timestamps and reasons.
   - A second run must have zero mutations.

5. [Establish authoritative live Mini health attestation](https://app.clickup.com/t/86e2gnq92) (`model:opus`, blocked on task 4 and prerequisite environment/skills/release/trust repairs)
   - Bind live PIDs to release SHA/tree, config schema and migration receipt, deployed-script manifest hashes, credential/skill preflight, execution-health counts, and scheduled-job health.
   - Exit nonzero for stale release, config mismatch, missing plist export, broken skill root, validator-chain failure, orphan execution, red job, or source/deployed mismatch.
   - Finish with launchd restart, inference smoke, cron smoke, and zero dead `running` records.

## Existing Prerequisites

These tasks have refreshed canonical briefs and remain independently executable where their current tags permit it:

- [Repair gateway/dashboard plist environment provenance](https://app.clickup.com/t/86e2eu44b)
- [Repair cron execution environment](https://app.clickup.com/t/86e2gdb3k)
- [Restore skills bridge health](https://app.clickup.com/t/86e2eu48t)
- [Automate verified Mini release cuts](https://app.clickup.com/t/86e2gdfwc)
- [Preserve active skill trust invariant](https://app.clickup.com/t/86e2g1tv8)

## PR-Autonomy Integration

- [PR autonomy 1/3](https://app.clickup.com/t/86e2gh04e) first reconciles conflicting PR #129 against current `main` under supervised `model:fable` execution. It no longer has `needs-human` or `agent-ready`.
- [PR autonomy 2/3](https://app.clickup.com/t/86e2gh063) is blocked on PR autonomy 1/3 and the truthful-cron outcome task.
- [PR autonomy 3/3](https://app.clickup.com/t/86e2gh078) is blocked on PR autonomy 2/3, the environment/skills/release/trust repairs, and the final live health attestation.

The older manual stale-alert activation, manual stale-PR triage, and legacy no-PR-left-behind tasks are superseded and `prep-blocked`. They are records only and must not be executed in parallel.

## Implementation Contract

### Execution ledger

Extend the internal schema with owner token, lease expiry, heartbeat, terminal reason, terminal timestamp, and migration support. Finalization must be atomic/idempotent. A stale worker or duplicate finalizer must not overwrite a newer terminal fact. The classifier uses durable evidence rather than process-name guesses.

### Cron outcome contract

Use one typed/normalized internal outcome through all runner paths. Ensure that controller status, child process exit code, execution row, and stored scheduled-job status agree. Model-generated text is explanatory only. Capture preflight failures before invoking work and preserve advisory/non-failure states only when a job contract expressly permits them.

### Reconciliation

Create a snapshot before any mutation, produce a dry-run manifest with per-row evidence and proposed terminal state, and apply only rows whose owner process/release/lease evidence proves them dead. Never delete rows or automatically re-run side effects. Re-running the same reconciliation must produce no further changes.

### Live health verifier

Keep the verifier source controlled. It must validate running release provenance, schema/migration state, generated plist exports, skills roots, source/deployed hashes, validator chain, health counts, and scheduled-job results without exposing credentials. Treat every mismatch as nonzero failure.

## Required Validation

- Unit and migration coverage for ledger ownership/fencing, heartbeats, terminal idempotency, and stale classification.
- Fault injection for worker death, signal, timeout, restart, PID reuse, duplicate actors, partial deployments, malformed results, missing credentials/skills/workdirs, required-tool failures, and verifier false-green attempts.
- E2E checks using a temporary `HERMES_HOME` for configuration propagation, source/deployed manifest comparison, and process/cron contracts.
- One forced job run and two subsequent scheduled-cycle receipts for each repaired red job.
- Live Mini proof: release/hash verification, launchd restart, inference smoke, cron smoke, healthy validator chain, and zero dead execution rows still marked `running`.

## Delivery Rules

For each child task: claim only when it is eligible; implement in the stated branch or an equivalent scoped branch; run targeted and integration validation; commit, push, deploy via the repository’s sanctioned Mini release path, and use `in review` with a complete handoff packet. Do not mark any task complete. A validator must independently verify and leave the `ignite-validate:` proof before completion.
