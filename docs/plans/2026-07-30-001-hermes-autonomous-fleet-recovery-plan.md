> ⚠️ NON-AUTHORITATIVE BACKUP SNAPSHOT — DO NOT EXECUTE FROM THIS FILE
>
> Live ClickUp list [`901714465284`](https://app.clickup.com/9017245888/v/li/901714465284)
> is the sole authority for its tasks, comments, dependencies, tags, statuses,
> readiness, and validator verdicts. Any live ClickUp difference supersedes the
> affected content in this snapshot. Stop and refresh ClickUp on any mismatch;
> never repair, synchronize, or overwrite ClickUp from this document.
>
> This document grants no executor authorization and has no authority to make a
> task ready, change a dependency or status, move work to review, or complete
> anything. Proposed bodies below remain inert until deliberately written to
> ClickUp and then re-read live.
>
> Runtime, deployment, provider, credential, and CI assertions in this document
> are historical planning context only. They are not current evidence, secret
> storage, release receipts, credential proof, CI proof, or validation proof.
> No token, credential content, or token fingerprint belongs in this file.

# Hermes autonomous fleet recovery plan — non-authoritative snapshot

```yaml
snapshot_metadata:
  document_kind: non_authoritative_clickup_planning_snapshot
  reference_only: true
  captured_at: "2026-07-30T08:35:17-07:00"
  clickup_fresh_read_at: "2026-07-30 morning PDT"
  clickup:
    workspace_id: "9017245888"
    list_id: "901714465284"
    list_url: "https://app.clickup.com/9017245888/v/li/901714465284"
    authority: sole_source_of_truth
  provenance:
    planning_role: "Fable adversarial planning and review"
    original_model: "claude-fable-5"
    source_session: "8c35965c-6c04-4505-ae7e-4cb91da7850b"
    artifact_note: "No separate artifact was literally named a Fable plan; this snapshot preserves the approved plan derived from that session."
  authority_flags:
    executor_authorization: false
    readiness: false
    task_status: false
    dependencies: false
    completion: false
    secrets: false
    runtime_evidence: false
    deployment_evidence: false
    provider_evidence: false
    credential_evidence: false
    ci_evidence: false
    validation_evidence: false
  drift_rule: "Any later live ClickUp task, comment, dependency, tag, status, or validator change supersedes the affected snapshot content."
  document_to_clickup_sync: prohibited
  refresh_required_before_use: true
```

## Authority boundary

The live task description and newest live `ignite-validate:` verdict control
what is executable. Text resembling an execution brief in this file is only a
backup draft. In particular:

- A live `validate-failed` comment is the repair specification even if this
  snapshot says something different.
- A proposed `agent-ready`, `prepped`, model-floor, status, parent, or dependency
  is not real until it exists in ClickUp and a fresh read confirms it.
- `Execution-ready` lines inside the fenced bodies below are deliberately marked
  inert. They must not be parsed as queue authorization.
- Historical observations and expected proofs must be reproduced against the
  exact deployed SHA and current runtime before they can satisfy acceptance.
- Nothing in this file may be used to infer, reconstruct, compare, or validate
  secret material.

Repository references:

- [Fleet cutover runbook](../../machine-setup/fleet-config/CUTOVER.md)
- [Fleet-config README](../../machine-setup/fleet-config/README.md)
- [Live ClickUp list `901714465284`](https://app.clickup.com/9017245888/v/li/901714465284)

## Approved six-task reference plan

The only inert identifiers permitted for not-yet-created tasks in this snapshot
are the six `UNCREATED:` slugs below. They are stable document references, not
ClickUp IDs.

### 1. Durable ClickUp-to-swarm bridge

PROPOSED CLICKUP BODY — INERT UNTIL WRITTEN LIVE

```markdown
Inert ID: UNCREATED:rebuild-3-1-durable-clickup-swarm-bridge
Title: [REBUILD 3.1] Durable ClickUp↔swarm bridge and terminal reconciliation
Parent / existing-task mapping: proposed child of REBUILD 3, task 86e2jbbg4
Proposed status: to do
Proposed tags: rebuild-0729, lane:code, agent-avoid, prepped, model:opus
Model floor: model:opus
Proposed native dependencies: none

## ⚙️ Execution Brief

Goal: Make the ClickUp-to-internal-kanban boundary a durable, idempotent state
machine. Once a ClickUp task is claimed, persist an exact bridge attempt before
asynchronous work proceeds. A deterministic reconciler, not a long-lived outer
LLM poll loop, owns terminal observation and ClickUp writeback.

The bridge must bind ClickUp task ID, lane, claim-run ID, idempotency key,
root/worker/verifier/synthesizer IDs, attempt ID, revision/fence, timestamps,
current state, last error, and writeback marker. No state may be rounded from
unknown to done.

Crash policy:

- Every attempt is failed/recoverable until the exact synthesizer is done and
  the exact ClickUp comment plus In Review transition have a durable receipt.
- On restart, an internally consistent attempt resumes with the same attempt
  ID, claim-run, idempotency key, and swarm topology.
- If receipt, topology, ownership, or writeback facts disagree, quarantine the
  attempt with evidence. Start a replacement only after the old claim and any
  worker are proven dead; never run beside uncertain-live work.

Binary acceptance criteria:

- [ ] Bridge persistence uses an explicit schema and revision/fencing CAS.
- [ ] Claim plus swarm creation persists one complete bridge identity before
      the dispatcher releases its singleton lease.
- [ ] Repeating a ClickUp task/idempotency key returns the original intact
      attempt and never creates a duplicate topology.
- [ ] Aggregate status is done only when the exact synthesizer is done.
- [ ] Any required worker, verifier, or synthesizer that is blocked, failed, or
      cancelled immediately makes the bridge non-done with the exact card and
      reason; a downstream todo card cannot hide it.
- [ ] Done writes the exact synthesizer result and moves ClickUp to In Review
      exactly once using a durable marker.
- [ ] No executor path writes ClickUp Complete.
- [ ] Genuine human-only blockage and transient provider unavailability are
      distinguishable states with different retry behavior.
- [ ] Claim heartbeat and release are bound to the matching claim-run ID.
- [ ] Crash injection at every state boundary proves resume-intact behavior,
      quarantine of inconsistent attempts, and no duplicate comment, status
      transition, claim release, or swarm.
- [ ] Live verification uses fresh ClickUp state and exact runtime receipts;
      this document is not accepted as proof.

Execution-ready: YES — INERT SNAPSHOT ONLY; NOT AUTHORIZATION
```

### 2. Unified dispatcher admission and lane fairness

PROPOSED CLICKUP BODY — INERT UNTIL WRITTEN LIVE

```markdown
Inert ID: UNCREATED:rebuild-3-2-unified-dispatcher-admission
Title: [REBUILD 3.2] Unified dispatcher admission without content-lane starvation
Parent / existing-task mapping: proposed child of REBUILD 3, task 86e2jbbg4
Proposed status: to do
Proposed tags: rebuild-0729, lane:code, agent-avoid, prepped, prep-blocked, model:opus
Model floor: model:opus
Proposed native dependencies:
- UNCREATED:rebuild-3-1-durable-clickup-swarm-bridge

## ⚙️ Execution Brief

Goal: Replace competing same-schedule code/content executor jobs with one
short-lived production dispatcher protected by the existing shared singleton.
The dispatcher selects both lanes fairly, claims one task, commits the durable
bridge handoff, and exits without waiting for swarm completion.

Preserve the completed fence-recovery trust contract from 86e2jeu7c: expiry
alone never authorizes takeover, and recovery remains bound to exact job,
execution, owner, fence, expiry, PID, and PID-start-time evidence.

Crash policy:

- A dispatch attempt is failed/recoverable until its exact bridge handoff is
  committed.
- A restart resumes an intact pre-existing bridge attempt instead of claiming a
  second task instance.
- An inconsistent admission/bridge owner is quarantined; replacement waits
  until old ownership is proven dead.

Binary acceptance criteria:

- [ ] Exactly one production ClickUp swarm-dispatcher identity is enabled.
- [ ] The competing content executor identity and stale second-executor identity
      are retired from production admission.
- [ ] Queue selection considers code and content; urgent/high work is global,
      and a durable last-lane policy alternates otherwise when both are eligible.
- [ ] The poll gate wakes the unified dispatcher for eligible work in either
      lane.
- [ ] Admission SQLite uses bounded busy waiting/retry so simultaneous
      acquisition cannot surface a transient database-is-locked job failure.
- [ ] A proven active owner produces a non-error deferred outcome.
- [ ] A wake received while the dispatcher is active remains durable and is
      scheduled after release.
- [ ] Parallel tests prove one request is admitted, the other is deferred,
      neither is recorded as a lock/contention failure, and deferred work runs
      after release.
- [ ] Dead-owner and crash-midpoint recovery tests remain fail-closed and green.
- [ ] Job manifest, gate IDs, admission identity policy, and tests agree exactly.

Execution-ready: NO — blocked-on: UNCREATED:rebuild-3-1-durable-clickup-swarm-bridge — INERT SNAPSHOT ONLY
```

### 3. Worker concurrency and lifetime bounds

PROPOSED CLICKUP BODY — INERT UNTIL WRITTEN LIVE

```markdown
Inert ID: UNCREATED:rebuild-3-3-worker-bounds
Title: [REBUILD 3.3] Bound swarm concurrency, wall time, and stale-worker recovery
Parent / existing-task mapping: proposed child of REBUILD 3, task 86e2jbbg4
Proposed status: to do
Proposed tags: rebuild-0729, lane:code, agent-avoid, prepped, prep-blocked, model:opus
Model floor: model:opus
Proposed native dependencies:
- UNCREATED:rebuild-3-2-unified-dispatcher-admission

## ⚙️ Execution Brief

Goal: Prevent outer-dispatch completion, timeout, or restart from leaving
unbounded internal workers alive. Ship explicit Mini concurrency caps,
stage-specific wall limits, stale detection, and exact owner-safe recovery.

Initial proposed production bounds are max_spawn=2, max_in_progress=2,
max_in_progress_per_profile=1, worker=3600s, verifier=1200s, and
synthesizer=900s. Live values remain a ClickUp/runtime decision and must be
verified before deployment.

Crash policy:

- A worker attempt is failed/recoverable until its exact kanban terminal tool
  commits a matching run result.
- Restart resumes an intact live attempt and its workspace.
- Missing/inconsistent run, PID, claim, or workspace identity is quarantined.
  A replacement starts only after the prior worker is proven dead.

Binary acceptance criteria:

- [ ] Fleet configuration declares global and per-profile concurrency caps plus
      stale detection explicitly.
- [ ] Every swarm worker, verifier, and synthesizer stores a non-null wall limit.
- [ ] Multi-swarm tests never exceed either concurrency cap.
- [ ] Timeout records exact task/run/PID identity and attempts graceful
      termination before force-kill.
- [ ] No claim is released or requeued while a matching worker may still live.
- [ ] Confirmed-dead timed-out workers become recoverable under the configured
      failure limit; uncertain-live workers remain fenced/quarantined.
- [ ] Gateway restart resumes intact attempts and cannot create a duplicate
      subprocess beside uncertain ownership.
- [ ] Live smoke proves configured values and no orphan process after an
      injected timeout.

Execution-ready: NO — blocked-on: UNCREATED:rebuild-3-2-unified-dispatcher-admission — INERT SNAPSHOT ONLY
```

### 4. Fleet script reconciliation

PROPOSED CLICKUP BODY — INERT UNTIL WRITTEN LIVE

```markdown
Inert ID: UNCREATED:rebuild-7-1-fleet-script-reconciliation
Title: [REBUILD 7.1] Reconcile every enabled fleet job to governed script and skill bytes
Parent / existing-task mapping: proposed child of REBUILD 7, task 86e2jbbhj
Proposed status: to do
Proposed tags: rebuild-0729, lane:code, agent-avoid, prepped, model:opus
Model floor: model:opus
Proposed native dependencies: none

## ⚙️ Execution Brief

Goal: Make the recovery deploy fail closed unless every enabled job's referenced
script and skill exists in the governed release at the expected path and the
live Mini bytes match an explicit source. Do not use broad rsync or infer that a
successful config install deployed scripts.

Crash policy:

- A reconciliation attempt is failed/recoverable until every planned copy and
  verification has a durable per-file receipt.
- Restart resumes an intact receipt and skips already verified exact bytes.
- A partial, conflicting, or path-inconsistent receipt is quarantined before a
  clean replacement attempt; never call a partial deployment done.

Binary acceptance criteria:

- [ ] Machine-readable inventory covers every enabled jobs.json script and
      declared skill plus the required LaunchAgent wrappers.
- [ ] Each inventory row records repository source, release source, live
      destination, expected SHA-256, mode, and owning job IDs.
- [ ] Preflight fails before mutation on a missing source, destination escape,
      unresolved skill, duplicate ambiguous skill, or manifest/schema error.
- [ ] Deployment copies only explicit inventory entries using atomic replace,
      per-file backup, and receipt; no rsync or wildcard copy is used.
- [ ] Post-write verification compares exact live bytes/mode to the receipt.
- [ ] Failure injection proves restart resumes intact receipts and quarantines
      partial/inconsistent receipts without declaring success.
- [ ] A final read-only check reports zero missing, drifted, ambiguous, or
      ungoverned enabled-job dependencies.
- [ ] REBUILD 7 cannot pass from documentation, this snapshot, or config hashes
      alone; exact live file evidence is required.

Execution-ready: YES — INERT SNAPSHOT ONLY; NOT AUTHORIZATION
```

### 5. Deterministic executor health watchdog

PROPOSED CLICKUP BODY — INERT UNTIL WRITTEN LIVE

```markdown
Inert ID: UNCREATED:rebuild-8-1-executor-health
Title: [REBUILD 8.1] Deterministic executor, dispatcher, and bridge watchdog
Parent / existing-task mapping: proposed child of REBUILD 8, task 86e2jbbhx
Proposed status: to do
Proposed tags: rebuild-0729, lane:code, agent-avoid, prepped, prep-blocked, model:opus
Model floor: model:opus
Proposed native dependencies:
- UNCREATED:rebuild-3-1-durable-clickup-swarm-bridge
- UNCREATED:rebuild-3-2-unified-dispatcher-admission
- UNCREATED:rebuild-3-3-worker-bounds

## ⚙️ Execution Brief

Goal: Add a deterministic no-agent watchdog for the execution path that failed.
It runs on a short cadence and uses a direct established alert transport with a
delivery receipt; it does not depend on an LLM digest to notice fleet failure.

Crash policy:

- A watchdog pass is failed/recoverable until its full input snapshot and any
  required alert delivery receipt commit atomically.
- Restart resumes from an intact checkpoint without suppressing a new signal.
- An inconsistent checkpoint or delivery receipt is quarantined and replayed
  safely; unknown delivery is not rounded to delivered.

Binary acceptance criteria:

- [ ] Watchdog reads durable cron execution, admission, bridge, dispatcher, and
      kanban state without invoking an LLM.
- [ ] Distinct alarms cover content success age/zero completions, admission lock
      errors, deferral/denial rate, over-age lease, over-age bridge, blocked
      upstream stage, dispatcher heartbeat age, stale/orphan worker, and ClickUp
      writeback drift.
- [ ] Every alarm names exact job/task/card/run identities and one next action.
- [ ] Dedupe is bounded and a standing-red condition re-alerts on cadence.
- [ ] Alert delivery has its own receipt and independent failure probe.
- [ ] Failure injection proves every alarm trips, delivery is distinguishable
      from generation, and healthy recovery clears it.
- [ ] The observed failure shape that motivated this plan would fail the
      watchdog, but historical counts in this file are not accepted as proof.
- [ ] The periodic fleet digest remains summary-only and is not the sole alarm.

Execution-ready: NO — blocked-on: UNCREATED:rebuild-3-3-worker-bounds — INERT SNAPSHOT ONLY
```

### 6. Workspace-refresh 429 regression

PROPOSED CLICKUP BODY — INERT UNTIL WRITTEN LIVE

```markdown
Inert ID: UNCREATED:workspace-refresh-429-regression
Title: [REBUILD 8.2] Prove ClickUp workspace refresh survives 429 and publishes atomically
Parent / existing-task mapping: proposed child of REBUILD 8, task 86e2jbbhx; new regression slice, not a request to reopen any completed task
Proposed status: to do
Proposed tags: rebuild-0729, lane:code, agent-avoid, prepped, model:sonnet
Model floor: model:sonnet
Proposed native dependencies: none

## ⚙️ Execution Brief

Goal: Establish a durable regression contract for ClickUp workspace refresh:
rate limiting must retry according to server guidance/bounded backoff, partial
output must never replace the last known-good map, and stale-map health must be
visible.

Crash policy:

- A refresh attempt is failed/recoverable until the complete new map validates
  and atomically replaces the prior map with a matching receipt.
- Restart may resume/retry an intact attempt without losing the last known-good
  map.
- Partial JSON, mismatched receipt, or inconsistent temp state is quarantined
  and rebuilt; it is never published or labelled fresh.

Binary acceptance criteria:

- [ ] Unit tests cover 429 with Retry-After/rate-limit reset, bounded fallback
      backoff, eventual success, retry exhaustion, malformed response, and
      process interruption.
- [ ] Exhausted retries preserve the exact last known-good map and return a
      non-clean job outcome.
- [ ] Successful refresh validates the full payload before atomic replace and
      writes a matching freshness receipt.
- [ ] Partial/temp/inconsistent output is quarantined and cannot become the
      canonical map.
- [ ] A deterministic stale-age signal reaches the executor-health watchdog.
- [ ] Live verification uses a safe observed/canary path without intentionally
      abusing the ClickUp API or assuming this snapshot proves current behavior.

Execution-ready: YES — INERT SNAPSHOT ONLY; NOT AUTHORIZATION
```

## Proposed dependency graph

This graph is inert. Native ClickUp parent/dependency relationships must be
created deliberately and confirmed by a fresh read.

```text
86e2jbbg4  REBUILD 3 parent (never agent-ready)
├── UNCREATED:rebuild-3-1-durable-clickup-swarm-bridge
├── UNCREATED:rebuild-3-2-unified-dispatcher-admission
│   └── waits on rebuild-3-1
└── UNCREATED:rebuild-3-3-worker-bounds
    └── waits on rebuild-3-2

86e2jbbhx  REBUILD 8 parent (never agent-ready)
├── UNCREATED:rebuild-8-1-executor-health
│   └── waits on rebuild-3-1, rebuild-3-2, rebuild-3-3
└── UNCREATED:workspace-refresh-429-regression

86e2jbbhj  REBUILD 7 rollout/soak parent (never agent-ready until predecessors pass)
├── UNCREATED:rebuild-7-1-fleet-script-reconciliation
└── waits on live completion of:
    ├── 86e2jbbg4  REBUILD 3
    ├── 86e2jbbhx  REBUILD 8
    ├── 86e2jf6up  swarm lifecycle repair
    ├── 86e2jfpwp  profile credential routing/isolation
    └── 86e2jeu7e  release-poller retirement
```

Parent-child membership is not a substitute for native waiting-on dependencies.
Every successor remains `prep-blocked` until all exact native predecessors have
a live validator-owned completion.

## Existing-task update map

This is a proposed reconciliation map, not permission to mutate the tasks.

| Live task ID | Proposed role in the plan | Proposed handling |
|---|---|---|
| `86e2jbbg4` | REBUILD 3 parent for bridge/admission/bounds | Keep `to do`; make a blocked, never-ready parent; depend on its three proposed children; retain latest live validator failure as the repair authority. |
| `86e2jbbhj` | Final governed deploy, canaries, and soak gate | Keep blocked and never ready until all native predecessors pass; require exact release, runtime, lane, and watchdog evidence. |
| `86e2jbbhx` | Recovery-critical health parent | Keep blocked and never ready; parent the executor-health and workspace-refresh regression slices. Do not hide a fleet-wide monitoring program inside one implementation card. |
| `86e2jf6up` | Swarm-stage completion/prompt-conflict repair | Reuse as an independently executable repair; successful internal stages complete, while genuine blockers remain sticky. Live `validate-failed` evidence supersedes this summary. |
| `86e2jfpwp` | Profile credential routing/isolation repair | Reuse; require profile-root fail-closed resolution and live source proof without storing secret material here. |
| `86e2jeu7e` | Executable release-poller retirement | Reuse; retirement must unload/archive/verify idempotently rather than exist only in documentation. |
| `86e2jbbgf` | Root fallback drill | Keep separate from this recovery graph. It does not authorize content fallback and should not gate executor liveness repair. |
| `86e2jeu7c` | Completed fence-recovery contract | Do not reopen. Treat its exact dead-owner proof requirements as non-regression constraints. |
| `86e2jep4c` | In-progress PR-staleness wrapper | Leave untouched and outside the recovery dependency graph. |

No status or tag in this table is claimed to be current after
`clickup_fresh_read_at`.

## Resume-intact, quarantine-and-restart policy

This policy applies to every proposed task and must be implemented in each
affected state machine, not merely copied into documentation.

1. **Exact done only.** An attempt remains failed/recoverable until its
   task-specific terminal facts and durable receipts all agree. A process exit,
   model response, saved file, merged commit, or partial write is not exact done.
2. **Resume intact.** After restart, resume the same attempt when its attempt ID,
   owner/fence, input identity, workspace, child topology, and last committed
   transition are internally consistent.
3. **Quarantine inconsistency.** If identity, ownership, topology, receipt, or
   state facts conflict, write a non-secret quarantine record describing the
   mismatch and prevent the attempt from progressing.
4. **Restart only after proof.** Start a clean replacement only after the old
   owner/worker/claim is proven dead or explicitly and safely retired. Never
   create a duplicate beside uncertain-live work.
5. **Never infer success.** Unknown, timeout, partial output, missing receipt,
   ambiguous delivery, or stale evidence remains non-done.
6. **Idempotent external effects.** ClickUp comments/status changes, alerts,
   claim releases, file promotion, and swarm creation require stable
   idempotency markers and replay-safe reconciliation.

## Explicit exclusions

- This snapshot is not a seventh task, parent epic, execution packet, deployment
  runbook, or validator handoff.
- Do not create any numeric ClickUp ID for an uncreated task. Only ClickUp may
  return real IDs; until then use the six exact `UNCREATED:` slugs.
- Do not replace the shared singleton with concurrent code/content executors.
  The proposed change shortens and unifies its critical section.
- Do not add a new core model tool for this work. Prefer scripts, CLI, skills,
  existing kanban surfaces, and a no-agent reconciler/watchdog.
- Do not restore the automatic release poller as part of recovery.
- Do not make the root fallback drill a prerequisite for content; content stays
  Sonnet-only and fail-closed.
- Do not reopen the completed fence-recovery task from this document.
- Do not absorb the in-progress PR-staleness wrapper into recovery.
- Do not treat historical runtime counts, prior CI, earlier credential checks,
  or this plan's expected values as current acceptance proof.
- Do not place secrets, credential contents, token fingerprints, or provider
  tokens in task bodies, receipts, comments, or this document.

## Live reconciliation checklist

Before using any part of this snapshot:

- [ ] Fetch ClickUp list `901714465284` fresh, bypassing local cache.
- [ ] Fetch each real task in the update map and its newest comments fresh.
- [ ] Record the newest `ignite-validate:` verdict for each task.
- [ ] Compare live name, description, parent, native dependencies, tags, model
      floor, status, priority, and assignee against this snapshot.
- [ ] If any field differs, mark the affected snapshot section superseded and
      follow live ClickUp; do not write document values back automatically.
- [ ] Confirm every proposed new task is still absent by title/slug before
      creating it.
- [ ] Create new tasks only through the governed ClickUp path; replace an inert
      slug with the real returned ID only in a later reference snapshot.
- [ ] Re-read every created/updated task fresh and confirm ClickUp preserved the
      full description, tags, parent, and dependencies.
- [ ] Confirm exactly one model-floor tag per executable task.
- [ ] Confirm every successor with an open predecessor has `prep-blocked`, no
      `agent-ready`, and a canonical live blocked execution brief.
- [ ] Confirm parent tasks are never agent-ready.
- [ ] Confirm executor-owned terminal status is In Review with a generated
      handoff packet; Complete remains validator-owned.
- [ ] Reproduce runtime, deploy, provider, credential-source, CI, and delivery
      evidence live against the exact shipped SHA before validation.
- [ ] Run a secret scan over task bodies, comments, receipts, and artifacts
      before attaching or publishing them.

## Document safety checks

- The first nonblank/rendered line must remain exactly:
  `> ⚠️ NON-AUTHORITATIVE BACKUP SNAPSHOT — DO NOT EXECUTE FROM THIS FILE`.
- All `authority_flags` must remain present and `false`.
- No automation, executor, prep pass, or validator may consume this file as an
  authoritative queue or synchronize it to ClickUp.
- Every real task reference must be a live ClickUp ID; every not-yet-created
  reference must be one of the six declared inert slugs.
- Proposed bodies must remain fenced and preceded by
  `PROPOSED CLICKUP BODY — INERT UNTIL WRITTEN LIVE`.
- Git review must reject removing or weakening the warning/metadata separately
  from the plan body.
