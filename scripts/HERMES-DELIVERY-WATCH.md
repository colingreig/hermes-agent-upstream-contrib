# Hermes delivery watcher

`hermes_delivery_watch.py` is an independent, zero-agent MacBook observer. Every
five minutes it reads normalized evidence, joins it through `task_delivery/v1`,
evaluates delivery and ownership invariants, and records evidence under:

```text
~/.hermes/state/task-delivery-watch/
  checkpoint.json       current open incidents
  heartbeat.json        last successful watcher poll
  events.jsonl          append-only poll and correlation evidence
  incidents.jsonl       append-only incident open/close transitions
  final-evidence.json   latest 24-72 hour acceptance result
```

It has no mutation or repair path. Its collectors are limited to local file
reads, HTTPS `GET`, and `ssh HOST cat -- /absolute/path`. It cannot claim a
task, update ClickUp, restart Hermes, merge a PR, release a lease, or promote a
release. Slack is notification-only and is deduplicated by incident transition.

## Configuration

Add a `delivery_watch` object to `~/.hermes/config.yaml`. JSON is also accepted
as a YAML-compatible operational config. Each collector returns an object with
`tasks` and/or `watch`.

```yaml
delivery_watch:
  slack_target: "slack:hermes"
  deadman_url: "https://hc-ping.com/REDACTED"
  collectors:
    - name: macbook-snapshot
      kind: file
      path: "/Users/colingreig/.hermes/state/delivery-input/macbook.json"
    - name: mini-snapshot
      kind: ssh-json
      host: mini
      path: "/Users/colingreig/.hermes/state/delivery-input/mini.json"
    - name: governed-read-api
      kind: http-json
      url: "https://example.invalid/task-delivery-snapshot"
      header_env:
        Authorization: DELIVERY_WATCH_AUTHORIZATION
```

An HTTPS collector uses only `GET`. Header values are resolved from launchd's
environment; do not put tokens in the config. A failed collector is
`UNKNOWN` and prevents a delivery result from becoming `DELIVERED`.

Each task snapshot has this normalized shape:

```json
{
  "task": {"id": "TASK-1", "lane": "mini"},
  "sources": {"clickup": {"status": "OK"}, "github": {"status": "OK"}},
  "executor": {"job_id": "job-1", "run_id": "run-1", "fencing_token": "17"},
  "ledger": {
    "execution_id": "exec-1",
    "job_id": "job-1",
    "run_id": "run-1",
    "fencing_token": "17"
  },
  "repository": "owner/repo",
  "pull_requests": [{"number": 123, "head_sha": "abc", "stack_index": 0}],
  "ci": {
    "runs": [
      {"run_id": 456, "head_sha": "abc", "status": "completed", "conclusion": "success"}
    ]
  },
  "handoff": {"id": "handoff-1", "head_sha": "abc"},
  "validator": {"identity": "validator-1", "verdict": "PASS", "head_sha": "abc"},
  "deployment": {"target": "mini", "head_sha": "abc"},
  "release": {"authority": "mini-release-cut", "receipt_id": "receipt-1", "head_sha": "abc"}
}
```

For a stacked delivery set `stacked: true`, list every PR in stack order, and
provide terminal successful CI for each exact head SHA. For a no-PR lane, the
snapshot must explicitly set `allow_no_pr: true` and include
`no_pr_authority` with `authority`, `receipt_id`, and `head_sha`. Omitting the
PR is never interpreted as authorization.

The optional `watch` object carries:

- `owners`: task/run/fencing, claim, heartbeat, expiry, budget, PR/CI/review and
  validator timestamps;
- `queue`: task, eligibility timestamp, and owner run;
- `review_gate`: `status` and `consecutive_clean_runs`;
- `lifecycle_events`: stable `id` and boolean `valid`;
- `promotions`: `prod_sha`, `certified`, `receipt_id`, and exact `receipt_sha`.

The watcher alerts on duplicate/unfenced ownership, missing lease heartbeats,
execution budget overruns, unowned eligible work after 40 minutes, claim to PR
after 90 minutes, PR to exact-head terminal CI after 45 minutes, green CI to
review after 20 minutes, review to validator after 90 minutes, review-gate
failures, invalid lifecycle events, uncertified promotions, and missing or
wrong-SHA release receipts.

## Operations and acceptance

Install but do not load:

```bash
scripts/install-hermes-delivery-watcher.sh --install
```

After reviewing the config, use `--install-and-enable`. The launchd job runs
`--once` every 300 seconds. Useful read-only commands are:

```bash
python3 scripts/hermes_delivery_watch.py --status
python3 scripts/hermes_delivery_watch.py --final-evidence
python3 scripts/hermes_delivery_watch.py --once --no-alert --no-deadman \
  --snapshot /absolute/path/to/offline-snapshot.json
```

Final evidence passes only after 24-72 hours with at least three distinct
delivered task chains, distinct executor runs/fences and PR/CI identities, no
ownership/lifecycle/promotion violation, three consecutive clean review-gate
runs, no poll gap or stale heartbeat beyond ten minutes, and no unresolved
alert or unknown source in the final 12 hours.
