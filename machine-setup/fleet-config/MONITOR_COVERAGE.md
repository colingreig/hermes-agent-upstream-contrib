# Hermes Mini monitor coverage

Task: ClickUp `86e2jbbhx`

Contract: `machine-setup/mini-scripts/fleet_outcome_contracts.json`

Probe: `machine-setup/mini-scripts/fleet_outcome_probe.py`

## What “covered” means

An enabled job is covered only when the independent probe can verify all of:

1. the exact declared job/LaunchAgent is present in the expected enabled,
   loaded, disabled, or retired state;
2. its last run is inside a cadence-specific freshness budget; and
3. a semantic output, endpoint, or durable receipt proves the task reached its
   intended outcome. A scheduler or launchd exit code by itself never passes.

The probe fails closed when a new enabled cron or Hermes/Ignite LaunchAgent has
no contract. Findings go directly to `hermes send --to slack:hermes`; alert
signatures and recoveries are persisted only after confirmed delivery.

`fleet_outcome_probe.py --drill-all --real-alert` creates an isolated fixture
for every row below and trips that row's real missing/stale/unloaded predicate
through the production formatter, Slack sender, receipt writer, and dedupe
state. It does not change live cron or launchd state. The five-minute
`ci-health-watch-cron.py` separately alarms if the probe receipt stops updating,
while the probe verifies CI health's semantic `"health": "OK"` result. These
two scheduling planes therefore cross-watch rather than self-monitor.

## Cron fleet: 16/16 declared

| ID | Job | Expected | Outcome proof (not exit-only) | Alarm condition |
|---|---|---:|---|---|
| `62714b869845` | clickup-executor | enabled | fresh saved response; rejects empty/failed turns | missing/stale/failed response |
| `dcab830aa41c` | content-lane-executor | retired/rejected | no live admission or dispatch | any enabled/runtime appearance is drift |
| `9dca144ff19b` | clickup-poll-gate | enabled | fresh `wakeAgent`/semantic-silent gate document | credential, scan, stale, or failed gate |
| `8d3b1d53470d` | review-poll-gate | enabled | fresh `wakeAgent`/semantic-silent gate document | credential, scan, stale, or failed gate |
| `ad0ae6b717e2` | clickup-review-sla | enabled | fresh SLA/review scan document | scan, stale, or failed result |
| `5a76e290811d` | hermes-pr-validate | enabled (2026-07-31, task `86e2k3qe1`) | fresh `ignite-validate` PASS/FAIL result document | error, missing, stale result, or safety-guard ABORT |
| `b0c4c5cc70c1` | spend-meter | enabled | fresh under-cap or spend evaluation document | unreadable spend data, stale, or failure |
| `e835c614cfb2` | ci-health-watch | enabled | fresh parsed CI state: stable lifecycle, VM available, no resource drift | unavailable/drifted/missing/stale state |
| `bcf275768661` | clickup-workspace-refresh | enabled | fresh parsed `clickup-map.json` topology artifact | missing/stale/invalid artifact |
| `dd73a5e578e4` | reap-stranded-claims | enabled | fresh claim/reap result document | error, missing, or stale result |
| `542fca8d839f` | ignite-board-sync | enabled | fresh sync/complete result document | failed/missing/stale sync |
| `2ff001bea4b5` | clickup-closeout-actor | enabled | fresh PR and DB closeout count documents | error, failed flip, missing/stale counts |
| `777876d3eb16` | clickup-lifecycle | enabled | fresh non-empty lifecycle response | empty/failed/stale lifecycle pass |
| `f23a03e9d1b2` | fleet-health-digest | enabled | fresh parsed delivery receipt with `status=sent` | missing/stale/failed delivery |
| `59bdd8ebc468` | repo-maintenance | enabled | fresh non-empty maintenance response | empty/failed/stale maintenance pass |
| `6e25865a22a4` | Purelymail notify-me poller | enabled | fresh `purelymail-poller.log` production finish (`dry_run=False` start + finished); rejects SSL/mailbox connect failures | missing/stale log, SSL/CERTIFICATE_VERIFY_FAILED, Mailbox poll failed, or Traceback |

## LaunchAgent fleet: 21 active + 1 retired

| Label | Expected | Outcome proof (not exit-only) | Alarm condition |
|---|---:|---|---|
| `ai.hermes.codex-proxy` | loaded | live TCP connect to loopback `:8646` | unavailable endpoint or unloaded agent |
| `ai.hermes.gateway` | loaded | semantic HTTP `127.0.0.1:8642/health` response with `status=ok` and `platform=hermes-agent` | endpoint/semantic failure or unloaded agent |
| `com.colingreig.hermes-dashboard` | loaded | semantic HTTP `:9119/health` response | endpoint failure or unloaded agent |
| `com.colingreig.hermes.daily-spend-alert` | loaded | fresh explicit OK/alert evaluation log | stale/fatal/delivery failure |
| `com.colingreig.hermes.degraded-secrets-monitor` | loaded | fresh healthy/alerted/recovered result; delivery-aware dedupe | stale result or incomplete alarm delivery |
| `com.colingreig.hermes.heartbeat` | loaded | fresh `heartbeat ping delivered` record | DORMANT, failed, or stale heartbeat |
| `com.colingreig.hermes.ignite-sentinel` | loaded | fresh monitor-start plus semantic JSON result | fatal/stale/missing monitor result |
| `com.colingreig.hermes.ignite-sentinel-digest` | loaded | fresh digest result/delivery marker | fatal/timeout/stale/missing digest |
| `com.colingreig.hermes.runtime-artifact-cleanup` | loaded | fresh completion summary | removal error or stale/missing summary |
| `com.colingreig.hermes.worktree-backstop-sweep` | loaded | fresh sweep summary and prune completion | safety/removal error or stale summary |
| `com.colingreig.ignite-marketplace-sync` | loaded | fresh `sync.sh exited 0` record | non-zero, missing, or stale sync |
| `com.hermes.offbox-restic-backup` | loaded | fresh post-restic `backup and retention complete` marker | backup/delivery failure or stale marker |
| `com.hermes.opendesign` | loaded | semantic HTTP `127.0.0.1:7456/api/health` response with `ok=true` and a version | endpoint or semantic failure, or unloaded agent |
| `com.colingreig.hermes.mcp-serve-reaper` | loaded | fresh sweep summary; snapshot/reap errors now exit non-zero | snapshot/reap failure or stale summary |
| `com.colingreig.ignite-skills-pull` | loaded | fresh parsed commit-pinned success receipt | stale/missing/invalid receipt |
| `com.colingreig.pull_anthropic_skills` | loaded | fresh parsed commit-pinned success receipt | stale/missing/invalid receipt |
| `com.colingreig.chrome-cdp` | loaded | semantic `/json/version` response with CDP WebSocket URL | endpoint failure or unloaded agent |
| `com.colingreig.hermes.usage-alert` | loaded | fresh durable receipt proving a clean scan, deduped alert, or confirmed Slack delivery | stale/missing receipt or failed delivery |
| `com.colingreig.hermes.fleet-outcome-probe` | loaded | current process execution plus CI-wrapper heartbeat cross-watch | unloaded agent or stale probe receipt |
| `com.colingreig.hermes.disk-space-alert` | loaded | fresh durable receipt with `status=ok` | low disk, check error, failed delivery, or stale/missing receipt |
| `com.colingreig.hermes.kanban-workspace-sweep` | loaded | fresh complete `sweep-finish` summary with `errors=0` | missing/unusable board DB, unlistable workspace root, non-zero errors, traceback, or stale/incomplete summary |
| `com.colingreig.hermes.release-poll` | retired | plist absent and `launchctl print` fails | any plist/load resurrection |

The legacy `com.ignite.skills-sync` label remains retired. If its plist
reappears, the probe's unknown-Hermes/Ignite fail-closed inventory check alarms
even though the label is not part of the active contract.

## Alarm proof

Repository tests prove semantic failures are distinguishable from scheduler
success, unknown enabled jobs and monitored plists fail closed, a failed Slack
send does not advance dedupe, confirmed delivery dedupes a repeat, and the
drill emits exactly one finding for every declared contract by
executing its real predicate.
The content-addressed `fleet_outcome_manifest.json` and
`reconcile_fleet_outcomes.py` deploy the scripts, contracts, two LaunchAgents,
and CI cron wrapper field as one scheduler-locked, snapshot-backed transaction.
The normal Mini release cut invokes this reconciler and rolls it back with the
release if byte, cron, launchd, or registration verification fails.

The live Mini cutover receipt and Slack drill receipt are attached to ClickUp
task `86e2jbbhx`; they are intentionally operational evidence rather than
hard-coded into this durable source document.
