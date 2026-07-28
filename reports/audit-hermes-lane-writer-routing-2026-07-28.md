# Audit: Hermes lane and writer routing end to end

Date: 2026-07-28  
ClickUp task: `86e2hgg9j`  
Scope: audit and evidence only; no routing implementation changes

## TL;DR

- Hermes does not currently guarantee Sonnet-only content writing: normal Git content and its revision pass invoke the code cascade, and all 185 inspected writer ledger rows were `code:*` with zero Sonnet rows.
- Queue eligibility is also fail-open: the live claim path ignores `prep-blocked`, model floors, and native dependencies; the Mini snapshot exposed five unclaimed `model:opus` tasks plus one continuation to autonomous executors.
- The nominal DB content route is presently dead before its writer call because it hardcodes a missing Python interpreter; several DB failure paths also collapse infrastructure errors into misleading routing parks.
- Live governance and evidence layers have drifted: the content-lane cron inherits GPT-5.5, the automated validator is disabled, the writer verifier/manifest differ from the deployed release, and telemetry cannot reconcile lane, orchestrator, and final writer.
- No new action tasks were created: the existing dependency chain `86e2hgg9j → 86e2hggkb → {86e2hggx9, 86e2hgh7u}` remains the remediation path and its successors remain blocked pending validator PASS.

## Audit boundaries

In scope:

- ClickUp eligibility, wake, claim, lane, model-floor, and dependency routing.
- Initial Git delegation, content revision, retry, fallback, recovery, DB-backed generation, DB apply, and closeout.
- Effective writer provider/model resolution, explicit override behavior, Mini job configuration, deployed-copy drift, and telemetry.
- Safe, non-publishing negative controls through `ssh mini`.

Out of scope:

- Implementing or deploying a routing fix.
- Publishing content, invoking a real provider solely for the audit, changing cron/launchd state, changing Mini files, or moving any remediation task.
- Validator PASS/FAIL on this report.

## Intended-versus-observed route matrix

| Route / condition | Intended route | Observed route or state | Evidence status |
|---|---|---|---|
| `lane:code`, ordinary Git task | Independent code cascade; no unnecessary Sonnet | Automatic code route resolved to GPT-5.5 → GPT-5.4-mini → GLM-4.7 → MiniMax-M3 → Gemini Flash. Anthropic code fallbacks were disabled in the live resolution. | CONFIRMED |
| Git CONTENT, first writer pass | Claude Sonnet content cascade only | Installed flow classifies CONTENT but calls `opencode_exec.py` without `--content`; it therefore selects the code cascade. | CONFIRMED |
| Git CONTENT, bounded revision | Claude Sonnet content cascade only, with lane preserved | Revision command also omits `--content`; it selects the code cascade again. | CONFIRMED |
| DB-backed content | Claude Sonnet content cascade only, then guarded DB apply | `db_publish_task.py` is the only production caller that passes `--content`, but its hardcoded Python 3.11 path is missing. The route parks on a swallowed DB-read failure before reaching the writer. | CONFIRMED |
| Content with Sonnet disabled / no enabled content tier | Typed hard failure before writing | Pure resolver control returned `content:openai/gpt-5`. | CONFIRMED |
| Content with Sonnet enabled but provider unavailable at invocation time | Sonnet-only hard failure; no cross-provider fallback | Not invoked during this read-only audit. The one-tier cascade indicates hard failure, but live provider behavior remains untested. | UNKNOWN |
| `--content --model <non-Sonnet>` | Reject incompatible explicit override | Explicit `--model` wins before content-cascade resolution; no compatibility check runs. No governed production caller currently exercises this escape. | CONFIRMED |
| Missing lane tag | Fail closed or resolve from one canonical typed contract | STEP ZERO accepts `agent-ready` with no lane; writer has only a Boolean content switch and defaults to code. | CONFIRMED |
| Conflicting `lane:code` + `lane:content` | Reject before claim | STEP ZERO fixture returned eligible. | CONFIRMED |
| Hybrid content/code task | Explicit deterministic hybrid policy; content-bearing work must not silently use code | No typed hybrid writer mode exists; Boolean `--content` cannot represent it, and the claimant does not inspect lane conflicts. | CONFIRMED |
| Retry after writer failure | Preserve the original resolved lane and retry inside that route | Task re-enters the same generic Git flow; for content that means the same no-`--content` call. Provider failover stays inside whichever cascade was selected initially. | CONFIRMED |
| Stranded-commit recovery | Do not invoke a writer; push/PR the existing commit | Installed recovery flow skips the writer. | CONFIRMED CLEAN PATH |
| Code route with Sonnet | Do not use editorial Sonnet unless an explicit compatible recovery policy allows it | Current automatic code route did not include Sonnet, but explicit `--model anthropic/claude-sonnet-5` and the separately gated Anthropic code tail remain supported. | CONFIRMED, NOT CURRENTLY AUTOMATIC |
| `model:opus` / `model:fable` autonomous claim | Reject; only Haiku/Sonnet floors may enter Hermes | Live claim gate ignores model floors. Mini snapshot exposed five unclaimed `model:opus` tasks and one continuation; executor-2 is GPT-5.4-mini. | CONFIRMED |
| `prep-blocked` or unmet native predecessor | Reject before wake and again before claim | Poll/claim code and installed skill contain no such gate. | CONFIRMED |
| Scheduled content executor | Content orchestration aligned with Sonnet-only, fail-closed policy | Enabled `content-lane-executor` runs `/ignite-execute --lane content` with inherited `openai-codex/gpt-5.5`, `no_fallback=false`; its resolved fallback list was empty at observation time. This is the orchestration layer, separate from the final writer layer. | PARTIAL: WRONG PRIMARY CONFIRMED; ACTIVE FALLBACK NOT OBSERVED |

## Telemetry reconciliation

### Writer ledger

The read-only ledger pass inspected `~/.hermes/logs/writer-served.jsonl` from `2026-07-19T13:37:53Z` through `2026-07-28T15:54:04Z`:

| Measure | Observed |
|---|---:|
| Rows | 185 |
| Unique tasks | 83 |
| `code:*` cascade rows | 185 |
| `content:*` cascade rows | 0 |
| GPT-5.5 served | 180 |
| GPT-5.4-mini served | 3 |
| Gemini Flash served | 2 |
| Claude Sonnet served | 0 |
| Successful outcomes | 166 |
| Failed outcomes | 19 |

This does not prove that every historical row was content-bearing. It does prove that the inspected writer telemetry contains no evidence of a Sonnet content route.

### Research-to-writer join

`research-served.jsonl` contained 33 rows for 28 task IDs. Twenty-one of those task IDs joined to 37 writer rows:

| Joined writer model | Rows |
|---|---:|
| GPT-5.5 | 34 |
| GPT-5.4-mini | 2 |
| Gemini Flash | 1 |
| Claude Sonnet | 0 |

All 37 joined rows were `code:*`. Research stage is the strongest available content-path proxy, but it is not a substitute for an explicit lane field. The absence of `resolved_lane`, writer mode, executor job/run ID, and invocation ID prevents authoritative per-route attribution.

### Cron/orchestrator versus final writer

The active executor jobs and final writer records describe different layers:

| Layer | Observed |
|---|---|
| Primary executor | job `62714b869845`, `openai-codex/gpt-5.6-sol`, every 30 minutes |
| Secondary executor | job `baa3251e033d`, `openai-codex/gpt-5.4-mini`, daily |
| Content-lane executor | job `dcab830aa41c`, inherited `openai-codex/gpt-5.5`, `no_fallback=false` |
| Final writer ledger | mostly `openai-codex/gpt-5.5`; no lane/job/run identifiers |

Current telemetry cannot prove which executor job caused an individual writer invocation.

## Findings

### 1. Git content and revision are sent to the code writer

Severity: **Critical**  
Status: **CONFIRMED**

The installed queue-poller classifies CONTENT in:

- `~/.hermes/skills/clickup-queue-poller/references/executor-main-flow.md:86-90`
- `~/.hermes/skills/clickup-queue-poller/references/content-quality-gate.md:20-41`

Its initial writer command omits `--content`:

- `executor-main-flow.md:114-124`
- `executor-open-code-handoff.md:54-59`

The content revision command also omits it:

- `content-quality-gate.md:154-166`

`machine-setup/mini-scripts/opencode_exec.py:599-601,653-662` defaults content mode false unless the CLI flag or `OPENCODE_CONTENT` exists. A live search found no alternate installed caller or global environment injection; the DB helper was the only production `--content` caller. Telemetry corroborated this: every research-linked writer row used `code:*`.

Recommended remediation: route one explicit typed writer mode through initial delegation and revision; reject missing or contradictory values before the provider call. Existing task: `86e2hggkb`.

### 2. Autonomous eligibility ignores readiness, model floors, and dependencies

Severity: **Critical**  
Status: **CONFIRMED**

The supervised Prep contract requires a ready brief, no `prep-blocked`, exactly one allowed model floor, and validator-complete predecessors. The live gates do not:

- `scripts/clickup_poll_gate.py:402-449` omits `prep-blocked`, `model:*`, and native dependency checks.
- `machine-setup/mini-scripts/clickup-queue-poller-claim_next.py:193-230` selects `to do` + `agent-ready`, excluding avoid/fenced, merge, attempt-cap, and cooldown states only.
- Recursive inspection of the installed queue-poller skill found no readiness/model/dependency gate.

The Mini snapshot at `2026-07-28 09:01:11` exposed five unclaimed `model:opus` tasks (`86e2h8krj`, `86e2hdek1`, `86e2gmgcn`, `86e2gmgct`, `86e2gmgdh`), one `in progress` continuation (`86e2haz4z`), and two floorless ready tasks (`86e2gmpa5`, `86e2gj54d`).

Recommended remediation: one shared fail-closed evaluator at wake and on a fresh pre-claim read; require a canonical ready brief, no blocking tags, exactly one allowed Haiku/Sonnet floor, and validator-complete predecessors. Existing task: `86e2hggkb`.

### 3. Missing, conflicting, and hybrid lanes are accepted

Severity: **Critical**  
Status: **CONFIRMED**

Pure STEP ZERO fixture probes returned eligible for:

- `["agent-ready"]`
- `["agent-ready", "lane:code", "lane:content"]`
- both valid single-lane cases

The final writer accepts only Boolean `--content`; it has no typed `code | content | hybrid | ambiguous` contract. A missing signal defaults to code, and contradictory signals are not represented.

Recommended remediation: normalize one explicit writer mode before claim, reject missing/conflicting values, and encode a deliberate hybrid policy. Existing task: `86e2hggkb`; shared readiness/evidence normalization also belongs in `86e2hgh7u`.

### 4. DB-backed content is dead before the writer

Severity: **Critical**  
Status: **CONFIRMED**

Live `~/.hermes/scripts/db_publish_task.py:58` hardcodes:

```text
/Users/colingreig/.hermes/hermes-agent/venv/bin/python3.11
```

The path is not executable. It is used for DB reads (`:121`), the final `opencode_exec.py --content` call (`:264-267`), and DB apply (`:308-315`). The first failure is swallowed into an empty DB result and becomes a slug-lookup park before the writer is invoked.

Recommended remediation: resolve the supported Hermes runtime interpreter dynamically, verify executable/import readiness before claim, and add a non-publishing DB route preflight. Existing task: `86e2hggkb`; executable readiness contract: `86e2hgh7u`.

### 5. DB closeout can manufacture its own validator PASS

Severity: **Critical**  
Status: **CONFIRMED, CONDITIONAL**

Live `~/.hermes/scripts/db_closeout_actor.py:308-334` posts `ignite-validate: PASS` when recovering a stale FAIL, then `:335-344` flips the task to review. `db_publish_and_closeout.py:143-175` invokes this actor and treats a successful flip as wrapper success.

The actor moves only to review, not complete, and the PASS is conditional rather than universal. The boundary violation still exists: an executor-side closeout actor is manufacturing validator provenance.

Recommended remediation: publish factual DB/live evidence and stop at review; only an independently invoked validator may issue PASS. Existing task: `86e2hgh7u`.

### 6. Content does not fail closed when Sonnet is disabled

Severity: **High**  
Status: **CONFIRMED**

`opencode_exec.py:95-99` declares a Sonnet-only content cascade, but `:208-214` substitutes `openai/gpt-5` for any empty cascade. The pure resolver probe:

```text
HERMES_CONTENT_SONNET=0
```

returned:

```json
[["openai/gpt-5"], "content:openai/gpt-5"]
```

This verifies configuration resolution when Sonnet is disabled or no content tier is enabled. It does not prove behavior when Sonnet remains enabled but its provider fails at invocation time; that route was not called during the audit.

Recommended remediation: return a typed pre-write routing failure for an empty content cascade; never apply the generic code floor to content. Existing task: `86e2hggkb`.

### 7. Explicit model override bypasses content compatibility

Severity: **High**  
Status: **CONFIRMED**

`opencode_exec.py:653-661` handles explicit `--model` before content cascade resolution. A non-Sonnet model therefore wins even when `--content` is also supplied. Current governed callers avoid explicit model, so this is a supported escape rather than an observed cron path.

Recommended remediation: reject non-Sonnet explicit models in content mode, or require a separately authorized and audited break-glass control. Existing task: `86e2hggkb`.

### 8. DB failures are mislabeled as routing or absence outcomes

Severity: **High**  
Status: **CONFIRMED**

`db_publish_task.py:91-99` catches all ClickUp fetch exceptions as `None`; `:194-205` converts that to a site-routing park. `:102-126` catches DB exceptions/nonzero results as `[]`; `:226-234` reports that as no matching slug.

Credential, interpreter, transport, timeout, parser, subprocess, and genuine not-found states are therefore indistinguishable.

Recommended remediation: typed stage results and bounded transient retry; park only on positively confirmed unsupported site or absence. Existing task: `86e2hgh7u`.

### 9. DB apply can publish a partial generated payload

Severity: **High**  
Status: **CONFIRMED**

Live `db_apply.py:180-197` records and deletes unknown keys; `:198-209` proceeds when any recognized key remains; `:262-299` commits the surviving fields. A typo or required-but-unmapped field can disappear while other content publishes.

Recommended remediation: fail on unknown keys by default, or require an explicit narrow allowlist and a complete required-field contract before commit. Existing task: `86e2hgh7u`.

### 10. Telemetry reports the wrong route health

Severity: **High**  
Status: **CONFIRMED**

`opencode_exec.py:424-490` derives expected-primary and armed state from the code cascade and does not persist explicit lane/mode. `writer-served-monitor.py:33,101-104` hardcodes GPT-5.4 as “Codex-served,” while the current code primary is GPT-5.5.

The read-only monitor reported:

```text
STATUS: HEALTHY
Armed runs: 184
Codex-served: 0
Other-degraded: 5
```

The ledger independently contained 183 `served_provider=openai-codex` rows.

Recommended remediation: persist requested lane, resolved mode, configured and effective cascades, expected provider/model, observed provider/model, executor job/run ID, and invocation ID. Derive health from the selected route rather than a stale model literal. Existing task: `86e2hggkb`.

### 11. Automated review consumption is disabled

Severity: **High**  
Status: **CONFIRMED**

Fresh `jobs.json` projection found:

- enabled/healthy `review-poll-gate` job `8d3b1d53470d`
- disabled/error `hermes-pr-validate` job `5a76e290811d`, last error `KeyboardInterrupt`
- no alternative enabled validator consumer

This audit intentionally stops in review for a separate validator session, as requested. The live automatic producer/consumer system nevertheless has a disabled consumer.

Recommended remediation: repair or deliberately retire the automated validator and align the review gate with that decision. Existing task: `86e2hgh7u`.

### 12. Deployed routing governance is stale and partly unreproducible

Severity: **High**  
Status: **CONFIRMED**

The installed queue-poller skill is a Mini-only artifact:

- version `1.36.0`
- SHA-256 prefix `7ee2513f`
- no second canonical `clickup-queue-poller/SKILL.md` found under the searched repository/skills roots

Release-versus-live hashes also differed for:

- `verify-writer-chain.py`: release `d9cbb3d5…`, live `f26ab519…`
- `writer-chain.json`: release `27806858…`, live `65abd0a2…`

The live verifier shells out to `op read`; the release copy uses the SDK resolver. The live manifest declares GLM-5.2 failover, while the byte-current runtime uses GPT-5.4-mini then GLM-4.7, and the verifier checks only the cascade head.

Recommended remediation: source-control and transactionally deploy the full queue-poller bundle; compare every ordered writer tier and record source commit plus hashes in the deployment receipt. Existing task: `86e2hggkb`.

### 13. Retry accounting and fallback receipts are not stage-accurate

Severity: **Medium**  
Status: **CONFIRMED / PARTIAL**

- `clickup-queue-poller-claim_next.py:71-80` uses `count_attempts(...) > cap`; with a cap of five, count five remains eligible and the sixth execution is allowed.
- `opencode_exec.py:1054-1067` records writer success before DB apply. DB apply failures at `db_publish_task.py:300-333` do not record their stage or error; the attempt is counted but diagnosed as writer success.
- `opencode_exec.py:787-791` can filter GLM after the cascade label was constructed. Historical receipts showed GPT-5.4-mini → MiniMax while labels still included GLM. The key currently resolves, so this label defect is conditional.

Recommended remediation: define cap semantics at the boundary, finalize attempts in the outer lane orchestrator, persist terminal failing stage, and record configured versus effective cascades. Existing tasks: `86e2hggkb` and `86e2hgh7u`.

## Negative controls

| Control | Result |
|---|---|
| Missing lane tag | Eligible at STEP ZERO |
| Conflicting code/content tags | Eligible at STEP ZERO |
| Hybrid mode | Not representable by writer CLI |
| Git content initial call | No `--content`; code cascade |
| Git content revision | No `--content`; code cascade |
| Explicit non-Sonnet model with content | Accepted by resolver path |
| Sonnet disabled | Resolved to `content:openai/gpt-5` |
| Stale installed writer artifacts | Release/live hash mismatch detected |
| Active queue-poller prompt snapshot | Current installed `SKILL.md` metadata matched the active snapshot exactly (`mtime_ns=1785178274156965790`, size `61821`); four differing `SKILL.md.bak*` files were not loaded |
| DB-backed content runtime | Missing hardcoded interpreter; shell exit 127 in read-only existence probe |
| Unsupported/missing DB site | Site guard correctly parked |
| Configured DB site | Site guard correctly resolved |
| Retry boundary at 5 | Count 5 allowed; count 6 blocked |

No provider call, DB write, content publish, ClickUp claim, cron run, or job restart was used as a negative control.

## Reproducible probes

These probes are read-only and redact secret values.

### Installed Git-content calls

```bash
ssh mini 'nl -ba ~/.hermes/skills/clickup-queue-poller/references/executor-main-flow.md | sed -n "86,134p"'
ssh mini 'nl -ba ~/.hermes/skills/clickup-queue-poller/references/content-quality-gate.md | sed -n "154,166p"'
ssh mini 'grep -RInE --exclude-dir=logs --exclude-dir=sessions --exclude-dir=state --exclude-dir=worktrees --exclude-dir=cache --exclude="*.bak*" -- "OPENCODE_CONTENT|\"--content\"|--content[[:space:]]" ~/.hermes/skills ~/.hermes/scripts ~/.hermes/cron 2>/dev/null'
ssh mini 'nl -ba ~/.hermes/scripts/opencode_exec.py | sed -n "653,662p"'
```

### Missing and conflicting lane fixtures

```bash
ssh mini 'PYTHONDONTWRITEBYTECODE=1 python3 -B - <<'"'"'PY'"'"'
import importlib.util
import os

path = os.path.expanduser("~/.hermes/scripts/claim_next.py")
spec = importlib.util.spec_from_file_location("claim_next_probe", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def selected(task):
    tags = module._tags(task)
    return (
        task.get("status") == "to do"
        and "agent-ready" in tags
        and "agent-avoid" not in tags
        and "agent-fenced" not in tags
    )

fixtures = [
    {"id": "missing", "status": "to do", "tags": ["agent-ready"]},
    {"id": "conflict", "status": "to do", "tags": ["agent-ready", "lane:code", "lane:content"]},
    {"id": "code", "status": "to do", "tags": ["agent-ready", "lane:code"]},
    {"id": "content", "status": "to do", "tags": ["agent-ready", "lane:content"]},
]

for task in fixtures:
    print(task["id"], selected(task), module._tags(task))
PY'
```

### Disabled-Sonnet resolver

```bash
ssh mini 'cd ~/.hermes/scripts && HERMES_CONTENT_SONNET=0 ~/.hermes/releases/v0.18.2-895b7d571f16/venv/bin/python -B -c '\''import importlib.util,pathlib,sys,json; p=pathlib.Path("opencode_exec.py"); sys.path.insert(0,str(p.parent.resolve())); s=importlib.util.spec_from_file_location("oc",p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(json.dumps(m.resolve_writer_cascade(content=True)))'\'''
```

### Writer and research telemetry

```bash
ssh mini 'python3 -c '\''import json,pathlib,collections; b=pathlib.Path.home()/".hermes/logs"; w=[json.loads(x) for x in (b/"writer-served.jsonl").read_text().splitlines() if x.strip()]; r=[json.loads(x) for x in (b/"research-served.jsonl").read_text().splitlines() if x.strip()]; ids={x.get("task_id") for x in r}; j=[x for x in w if x.get("task_id") in ids]; C=lambda xs,k:dict(collections.Counter(str(x.get(k)) for x in xs)); print(json.dumps({"writer_rows":len(w),"first":w[0].get("ts"),"last":w[-1].get("ts"),"writer_models":C(w,"served_model"),"writer_cascades":C(w,"writer_cascade"),"research_ids":len(ids),"joined_rows":len(j),"joined_models":C(j,"served_model"),"joined_cascades":C(j,"writer_cascade")},sort_keys=True))'\'''
```

### Writer monitor and effective failover

```bash
# Snapshot-bound: future rows may change the counts after the recorded cutoff.
ssh mini 'python3 ~/.hermes/scripts/writer-served-monitor.py --window 185; printf "exit=%s\n" "$?"'
ssh mini 'python3 -c '\''import json,pathlib,collections; r=[json.loads(x) for x in (pathlib.Path.home()/".hermes/logs/writer-served.jsonl").read_text().splitlines() if x.strip()]; print(collections.Counter(" > ".join(a.get("model","") for a in x.get("failover_tried",[])) for x in r))'\'''
```

### Queue floor/readiness exposure

```bash
ssh mini 'python3 -c '\''import json,os; d=json.load(open(os.path.expanduser("~/.hermes/scripts/queue_snapshot.json"))); print([(t["id"],t.get("status"),t.get("kind"),[x for x in t.get("tags",[]) if x.startswith("model:")]) for t in d.get("tasks",[]) if any(x in t.get("tags",[]) for x in ("model:opus","model:fable","prep-blocked")) or not any(x.startswith("model:") for x in t.get("tags",[]))])'\'''
```

### Mini job routing

```bash
ssh mini 'python3 -c '\''import json,os; d=json.load(open(os.path.expanduser("~/.hermes/cron/jobs.json"))); j=next(x for x in d["jobs"] if x["id"]=="dcab830aa41c"); print({k:j.get(k) for k in ("id","name","enabled","prompt","model","provider","no_fallback","model_snapshot","provider_snapshot","route_health")})'\'''
ssh mini 'python3 -c '\''import json,os; d=json.load(open(os.path.expanduser("~/.hermes/cron/jobs.json"))); print([{k:j.get(k) for k in ("id","name","enabled","last_status","last_error","skill","model","provider")} for j in d["jobs"] if any(s in " ".join(str(j.get(x) or "") for x in ("name","skill","prompt")).lower() for s in ("validate","validator","review-poll"))])'\'''
```

### DB interpreter and failure boundaries

```bash
ssh mini 'p="$HOME/.hermes/hermes-agent/venv/bin/python3.11"; test -x "$p" && echo OK || echo MISSING:$p; grep -n "VENV_PY" ~/.hermes/scripts/db_publish_task.py'
ssh mini 'nl -ba ~/.hermes/scripts/db_publish_task.py | sed -n "91,126p;194,234p;259,333p"'
ssh mini 'nl -ba ~/.hermes/scripts/db_closeout_actor.py | sed -n "308,344p"'
ssh mini 'nl -ba ~/.hermes/scripts/db_apply.py | sed -n "163,209p;262,299p"'
```

### DB site-routing fixtures

```bash
ssh mini 'PYTHONDONTWRITEBYTECODE=1 python3 -B - <<'"'"'PY'"'"'
import importlib.util
import json
import os

path = os.path.expanduser("~/.hermes/scripts/db_site_config.py")
spec = importlib.util.spec_from_file_location("db_site_config_probe", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

fixtures = {
    "supported": {"name": "rewrite", "description": "https://dynamics365group.com/blog/valid-slug"},
    "unsupported": {"name": "rewrite", "description": "https://example.com/blog/valid-slug"},
    "missing": {"name": "rewrite", "description": "no URL"},
    "multi": {"name": "rewrite", "description": "source https://example.com/x target https://www.dynamics365group.com/blog/right-slug"},
}

for name, task in fixtures.items():
    config, park = module.guard_resolve_site(task)
    print(name, json.dumps({
        "cfg": config.domain if config else None,
        "park": bool(park),
        "domain": park.get("domain") if park else None,
        "slug": module.derive_slug(task, config) if config else None,
    }, sort_keys=True))
PY'
```

### Retry boundary

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=machine-setup/mini-scripts python3 -B - <<'PY'
import importlib.util

spec = importlib.util.spec_from_file_location(
    "claim_next_probe",
    "machine-setup/mini-scripts/clickup-queue-poller-claim_next.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

for count in (4, 5, 6):
    module.claim_store = type(
        "ClaimStoreProbe",
        (),
        {"count_attempts": staticmethod(lambda *args, observed=count: observed)},
    )
    print(count, module._over_attempt_cap("fixture-task", 5))
PY
```

### Deployment drift

```bash
ssh mini 'find ~/dev ~/.codex ~/.claude ~/.hermes/brain ~/brain -type f -path "*/clickup-queue-poller/SKILL.md" -print 2>/dev/null; shasum -a 256 ~/.hermes/skills/clickup-queue-poller/SKILL.md ~/.hermes/scripts/verify-writer-chain.py ~/.hermes/writer-chain.json ~/.hermes/releases/v0.18.2-895b7d571f16/machine-setup/mini-scripts/verify-writer-chain.py ~/.hermes/releases/v0.18.2-895b7d571f16/machine-setup/mini-scripts/writer-chain.json'
ssh mini 'PYTHONDONTWRITEBYTECODE=1 python3 -B - <<'"'"'PY'"'"'
import json
import os

skill = os.path.expanduser("~/.hermes/skills/clickup-queue-poller/SKILL.md")
snapshot = os.path.expanduser("~/.hermes/.skills_prompt_snapshot.json")
stat = os.stat(skill)
data = json.load(open(snapshot, encoding="utf-8"))
record = data["manifest"].get("clickup-queue-poller/SKILL.md")
current = [stat.st_mtime_ns, stat.st_size]
print({"snapshot": record, "current": current, "match": record == current})
PY'
```

## Dead ends and explicitly unknown states

- No real provider call was made solely for the audit. The disabled-Sonnet result is from the pure resolver, not a billed request. Enabled-but-runtime-unavailable Sonnet behavior remains untested.
- DB-publish/Sonnet live success remains unverified in the inspected window: there were no `content:*` ledger rows, and the current DB route stops at its missing interpreter.
- Writer records contain no lane/content mode, executor job/run ID, or invocation ID. Research-task joins are evidence of misrouting, not a replacement for typed telemetry.
- Only six of 202 inspected OpenCode log files contained the newer route metadata, so older attempt-level routing cannot be reconstructed reliably.
- No current `publish_result.json` marker was available for DB marker-to-ledger reconciliation.
- The Mini-only queue-poller skill has no repository source hash to compare against.
- The active queue-poller prompt snapshot matched the current installed `SKILL.md`; stale backup files were not loaded. Canonical source comparison remains unknown because no source copy was found.
- Actual ClickUp dependency enforcement was established from the claim code and installed-skill search; no destructive live claim control was attempted.
- The content-lane job had `no_fallback=false`, but its resolved fallback list was empty when observed; the report does not claim an active fallback tier.

## Existing remediation chain

No duplicate tasks were created.

| Task | Intended use of this report | Gate |
|---|---|---|
| `86e2hggkb` — Enforce Sonnet-only Hermes content writing with fail-closed routing | Typed eligibility/lane/writer mode; Git/revision/DB routing; explicit override and empty-cascade failures; telemetry; deployment governance | Native wait on this audit; remains `prep-blocked` until validator PASS |
| `86e2hggx9` — Gate Hermes SEO capability to SEO-relevant content work | Consume the landed typed writer contract; keep SEO capability opt-in and Sonnet-backed | Native wait on `86e2hggkb`; remains `prep-blocked` |
| `86e2hgh7u` — Make Hermes content QA readiness and evidence contract executable | Shared pre-claim readiness/evidence contract; DB failure typing; validator provenance; executable lifecycle checks | Native wait on `86e2hggkb`; remains `prep-blocked` |

## Method

| Phase | Coverage |
|---|---|
| Static contract | ClickUp eligibility, Prep/Executor contract, model floors, lane semantics |
| Static writer call graph | Initial/revision calls, Boolean content seam, explicit override, provider cascades |
| DB/recovery | DB read/generate/apply/closeout, retry, recovery, fallback |
| Live Mini | launchd/jobs, installed skills/scripts, version/hash drift, route configuration |
| Telemetry | writer/research ledgers, fallback receipts, route metadata, liveness monitor |
| Negative controls | missing/conflicting/hybrid lanes, disabled Sonnet, explicit override, DB interpreter/site, retry boundary, stale copies |
| Adversarial verification | Three independent refutation passes over every retained High/Critical finding |

The orchestrator was GPT-5.6 Sol. Three bounded Codex subagents performed six read-only dimensions in two waves and a third adversarial-refutation wave. Codex did not expose a per-call model selector, so the audit used capability-scoped prompts rather than claiming a model override that the runtime could not enforce.
