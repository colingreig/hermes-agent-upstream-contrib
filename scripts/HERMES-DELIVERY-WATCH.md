# Hermes delivery watcher

`hermes_delivery_watch.py` is an independent, zero-agent MacBook observer.
`hermes_delivery_snapshot.py` is its read-only live evidence producer. Every
five minutes launchd runs the producer first, then evaluates that exact
snapshot through `task_delivery/v1` and records evidence under:

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

The producer reads the AI Dev Assistant list through the existing ClickUp CLI,
PR and exact-head workflow evidence through authenticated `gh` read commands,
and Mini state through fixed `ssh mini` `cat`, `tail`, and read-only SQLite
queries. All subprocess argument vectors and remote paths are bounded in source;
there is no shell command from configuration and no agent invocation. It writes
only one atomic local snapshot:

```text
~/.hermes/state/delivery-input/macbook.json
```

Each upstream failure is retained as `collection.<source>.status=UNKNOWN`.
The watcher imports those nested source states, so a successful local file read
cannot mask a failed ClickUp, GitHub, SSH, ledger, lifecycle, or receipt read.

## Configuration

The installer creates a dedicated, mode-0600 configuration at
`~/.hermes/config.delivery-watch.yaml`. It never rewrites the main Hermes
configuration. JSON is accepted as YAML-compatible configuration. The generated
minimum is:

```yaml
delivery_snapshot:
  clickup_list_id: "901714465284"
  mini_host: mini
  lookback_hours: 72
  max_tasks: 40
delivery_watch:
  collectors:
    - name: live-delivery-snapshot
      kind: file
      path: "/Users/colingreig/.hermes/state/delivery-input/macbook.json"
```

After installation, optionally add `slack_target` and `deadman_url` beneath
`delivery_watch`. Do not put ClickUp, GitHub, Slack, or dead-man secrets in the
file. ClickUp uses the existing CLI token resolution, `gh` uses its existing
authentication, and an HTTPS collector resolves header values from launchd's
environment. A failed source is `UNKNOWN` and prevents a delivery result from
becoming `DELIVERED`.

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

This installs the producer, watcher, correlator, dedicated configuration, and
plist without loading the job. After reviewing the generated configuration,
use `--install-and-enable`. The launchd job runs the following cycle every 300
seconds:

```text
hermes_delivery_snapshot.py --once --run-watcher
  -> atomic live snapshot
  -> hermes_delivery_watch.run_once()
  -> append-only watcher evidence and heartbeat
```

Useful read-only commands are:

```bash
python3 scripts/hermes_delivery_watch.py --status \
  --config ~/.hermes/config.delivery-watch.yaml
python3 scripts/hermes_delivery_watch.py --final-evidence \
  --config ~/.hermes/config.delivery-watch.yaml
python3 scripts/hermes_delivery_snapshot.py --once \
  --config ~/.hermes/config.delivery-watch.yaml
python3 scripts/hermes_delivery_watch.py --once --no-alert --no-deadman \
  --snapshot /absolute/path/to/offline-snapshot.json
python3 scripts/hermes_delivery_snapshot.py --once \
  --config /absolute/path/to/offline-config.json \
  --fixture /absolute/path/to/source-fixture.json \
  --output /absolute/path/to/offline-snapshot.json
```

`--fixture` disables every live command and is the supported offline test and
incident-reproduction mode. It exercises repo-only, stacked-PR, explicit
no-PR, and Mini delivery shapes without secrets.

Final evidence passes only after 24-72 hours with at least three distinct
delivered task chains, distinct executor runs/fences and PR/CI identities, no
ownership/lifecycle/promotion violation, three consecutive clean review-gate
runs, no poll gap or stale heartbeat beyond ten minutes, and no unresolved
alert or unknown source in the final 12 hours.
