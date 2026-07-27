#!/usr/bin/env python3
"""autonomous_merge.py — the MERGE ACTOR that closes the autonomy loop.

The validator (hermes-pr-validate) WRITES PASS/BLOCK verdicts; merge_guard.py and
hermes_validate_ops.cmd_merge_pr GATE merge attempts. But until now nothing
actually ATTEMPTED the merge, so every PASS sat forever and the open-PR backlog
grew without bound ("build but never ship"). This script is that missing actor: a
deterministic, zero-LLM sweep that merges every PR the gates already permit.

For each verdict-store entry it merges iff ALL hold (default-deny, fail-closed):
  - VALIDATE_SHADOW is false           (shadow = validator observes, never merges)
  - tier autonomy is enabled           (HERMES_AUTONOMOUS_MERGE master, or the
                                         per-tier HERMES_AUTONOMOUS_MERGE_<TIER>)
  - verdict == PASS and fresh          (< DEFAULT_MAX_AGE_H)
  - the stored head_sha == the PR's CURRENT head (re-fetched live — a PASS for an
                                         older commit must NOT merge a newer one)
  - repo is on the allowlist
  - PR state is OPEN                    (merged/closed just drop out — idempotent)
  - the PR's own checks are not failing AND it is mergeable (no conflicts)

The merge itself goes through `hermes_validate_ops.py merge-pr` (which RE-enforces
allowlist + fresh-PASS verdict + shadow), executed with ~/.hermes/bin prepended to
PATH so the `gh` merge-guard shim (defense-in-depth + bot attribution) is always
used. After a successful merge it best-effort clears the `needs-validation` tag,
adds a `merged` tag, and posts a one-line ClickUp comment — none of which is fatal
to the merge if ClickUp auth hiccups.

Idempotent and safe on a cron tick. `sweep()` never raises.

CLI:
  autonomous_merge.py            # live sweep
  autonomous_merge.py --dry-run  # report what WOULD merge, touch nothing
  autonomous_merge.py --repo R --pr N   # restrict to one PR (still fully gated)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

if __package__:
    from . import validator_verdict
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import validator_verdict

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_BIN_DIR = os.path.expanduser("~/.hermes/bin")
PY = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python3.11")
VAL_OPS = os.path.join(SCRIPTS_DIR, "hermes_validate_ops.py")
ALLOWLIST_PATH = os.path.expanduser("~/.hermes/allowed-repos.txt")
# Same offline rename-equivalence file validator_repo_guard.py uses (its RC2
# header). Expands a loaded allowlist to every alias spelling of a repo that
# has been renamed, so a caller supplying either name still matches.
REPO_ALIASES_PATH = os.path.expanduser("~/.hermes/config/repo-aliases.json")


def _expand_repo_aliases(names, path=REPO_ALIASES_PATH):
    """See hermes_validate_ops._expand_repo_aliases — same tolerant behavior,
    duplicated here because this module does not import that one."""
    try:
        with open(path, encoding="utf-8") as f:
            groups = json.load(f).get("aliases") or []
    except Exception:
        return names
    expanded = set(names)
    for group in groups:
        if isinstance(group, list) and expanded.intersection(group):
            expanded.update(group)
    return expanded


GH_TIMEOUT = 60
POST_CLICKUP_COMMENT = os.path.expanduser("~/.hermes/scripts/post_clickup_comment.py")
_TRUTHY = {"1", "true", "yes", "on"}

# Check conclusions that mean "do not merge" — a real red on the PR head.
_BAD_CHECK = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
              "STARTUP_FAILURE", "STALE"}
# Conclusions that are fine to merge over.
_OK_CHECK = {"SUCCESS", "NEUTRAL", "SKIPPED", "", None}

# NON-GATING checks: prod-monitoring / post-deploy / preview checks whose RED
# reflects prod or deploy state, NOT whether THIS PR's code is sound. A failing
# one must NOT block a merge — same self-referential trap as ci_green_on_base
# (e.g. elevatoruptime's "Report-outage smoke test" fails forever on prod data
# while "Lint, typecheck, test" — the real code gate — is green). Matched as a
# lowercase SUBSTRING of the check name. Keep this conservative: only names that
# are clearly monitoring/deploy, never a unit/integration/build/lint/type gate.
_NONGATING_CHECK_SUBSTR = (
    "smoke", "report-outage", "report outage", "e2e", "lighthouse",
    "monitor", "cloudflare pages", "vercel", "preview", "uptime", "synthetic",
)


def _is_gating_check(name):
    low = (name or "").lower()
    return not any(s in low for s in _NONGATING_CHECK_SUBSTR)


def _env_truthy(name):
    return (os.environ.get(name, "") or "").strip().lower() in _TRUTHY


def _shadow():
    # Activation switch (Task: autonomous-merge activation). Default is
    # ACTIVATED (not shadow) unless HERMES_MERGE_SHADOW is explicitly truthy —
    # a single env-derived helper shared with merge_guard._shadow(),
    # hermes_validate_ops.VALIDATE_SHADOW, and the verdict writer's "shadow"
    # stamp (validator_verdict.merge_shadow_active()) so all four can never
    # disagree about which mode the pipeline is in.
    return validator_verdict.merge_shadow_active()


def _tier_autonomy_enabled(tier):
    """Master HERMES_AUTONOMOUS_MERGE enables ALL tiers; else the per-tier flag
    HERMES_AUTONOMOUS_MERGE_<LOW|MEDIUM|HIGH>. Unknown tier => HIGH (strictest).
    Mirrors merge_guard._tier_autonomy_enabled exactly."""
    if _env_truthy("HERMES_AUTONOMOUS_MERGE"):
        return True
    return _env_truthy("HERMES_AUTONOMOUS_MERGE_" + (tier or "high").upper())


def _load_allowlist(path=ALLOWLIST_PATH):
    result = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    result.add(s)
    except FileNotFoundError:
        pass
    return _expand_repo_aliases(result)


def _load_allowlist(path=ALLOWLIST_PATH):
    result = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    result.add(s)
    except FileNotFoundError:
        pass
    return _expand_repo_aliases(result)


def _shim_env():
    """Env with ~/.hermes/bin first so `gh` resolves to the merge-guard shim
    (bot attribution + defense-in-depth) regardless of who runs the sweep."""
    env = dict(os.environ)
    env["PATH"] = HERMES_BIN_DIR + os.pathsep + env.get("PATH", "")
    return env


def _post_clickup_comment(task_id, body):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", prefix="automerge_", delete=False, encoding="utf-8")
    try:
        tmp.write(body)
        tmp.close()
        r = subprocess.run(
            [PY, POST_CLICKUP_COMMENT, task_id, tmp.name],
            capture_output=True, text=True, timeout=60, env=_shim_env(),
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, f"guarded comment error: {e!r}"
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _gh_json(repo, pr, fields):
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo, "--json", fields],
            capture_output=True, text=True, timeout=GH_TIMEOUT, env=_shim_env())
        if r.returncode != 0:
            return None, f"gh pr view rc={r.returncode}: {r.stderr.strip()[:160]}"
        return json.loads(r.stdout or "{}"), None
    except Exception as e:
        return None, f"gh pr view error: {e!r}"


def _gh_api(path):
    """REST GET via gh api (App token). Returns (json, err). The REST checks API
    does NOT require actions:read, unlike the GraphQL statusCheckRollup that
    `gh pr view --json statusCheckRollup` / `gh pr checks` drill into — those fail
    with 'Resource not accessible by integration' under the Hermes App token."""
    try:
        r = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                           timeout=GH_TIMEOUT, env=_shim_env())
        if r.returncode != 0:
            return None, f"gh api {path} rc={r.returncode}: {r.stderr.strip()[:160]}"
        return json.loads(r.stdout or "{}"), None
    except Exception as e:
        return None, f"gh api error: {e!r}"


def _pr_state(repo, pr):
    """Return (info, reason). info has state, head, mergeable, merge_state, and
    the GATING failing/pending check names (monitoring smokes excluded). Uses the
    REST checks API (App-token-safe), not the GraphQL rollup. reason set only on
    a fetch error."""
    data, err = _gh_json(repo, pr, "state,headRefOid,mergeable,mergeStateStatus,isDraft,labels")
    if err:
        return None, err
    head = data.get("headRefOid") or ""
    failing, pending, ignored, gating_green = [], [], [], []
    # CheckRun checks (Actions, etc.) via REST — name + status + conclusion.
    if head:
        runs, rerr = _gh_api(f"repos/{repo}/commits/{head}/check-runs")
        if rerr:
            return None, rerr
        for c in (runs.get("check_runs") or []):
            name = c.get("name") or "?"
            concl = (c.get("conclusion") or "").upper()
            is_fail = (c.get("status") == "completed" and concl in _BAD_CHECK)
            is_pending = c.get("status") != "completed"
            gating = _is_gating_check(name)
            # A completed-SUCCESS gating check is real green CI on this code. Track
            # it so evaluate() can fail CLOSED when NO real gate ran (a repo with
            # only Vercel/preview/smoke checks would otherwise auto-merge unverified).
            if gating and c.get("status") == "completed" and concl == "SUCCESS":
                gating_green.append(name)
            if not (is_fail or is_pending):
                continue
            if not gating:
                ignored.append(name)
                continue
            (failing if is_fail else pending).append(name)
        # Legacy commit statuses (e.g. Vercel) via REST.
        stat, serr = _gh_api(f"repos/{repo}/commits/{head}/status")
        if not serr:
            for s in (stat.get("statuses") or []):
                name = s.get("context") or "?"
                st = (s.get("state") or "").upper()
                if st == "SUCCESS" and _is_gating_check(name):
                    gating_green.append(name)
                elif st in ("FAILURE", "ERROR"):
                    (ignored if not _is_gating_check(name) else failing).append(name)
                elif st == "PENDING":
                    (ignored if not _is_gating_check(name) else pending).append(name)
    return {
        "state": data.get("state"),
        "head": head,
        "mergeable": data.get("mergeable"),
        "merge_state": (data.get("mergeStateStatus") or "").upper(),
        "draft": bool(data.get("isDraft")),
        "labels": [(l.get("name") or "") for l in (data.get("labels") or [])],
        "failing": failing,
        "pending": pending,
        "ignored": ignored,
        "gating_green": gating_green,
    }, None


def evaluate(repo, pr, verdict, allowlist):
    """Pure-ish decision: return (action, detail). action in
    {'merge','skip','blocked'}. Network only via _pr_state (read-only)."""
    tier = verdict.get("tier", "high")
    shadow_plan_error = _shadow_merge_plan(verdict)
    if shadow_plan_error:
        return "blocked", shadow_plan_error
    if _shadow():
        return "skip", "VALIDATE_SHADOW is true (fenced MergeActor plan recorded; live ownership disabled)"
    if not _tier_autonomy_enabled(tier):
        return "skip", f"tier '{tier}' autonomy not enabled"
    return _merge_readiness(repo, pr, verdict, allowlist)


def _shadow_merge_plan(verdict):
    """Exercise the exact MergeActor contract without granting merge authority.

    This makes the vendored reconciler path a real runtime consumer of the
    immutable SQLite identity while ``_shadow()`` remains the unconditional
    ownership kill switch. ``MergeActor.merge(execute=False)`` returns before
    snapshot, checkout, credential, or pusher access.
    """
    raw_identity = verdict.get("identity")
    if not isinstance(raw_identity, dict):
        return "fenced SQLite verdict lacks immutable identity"
    try:
        if __package__:
            from .identity import TrustedMergeIdentity
            from .merge_actor import MergeActor
        else:
            from pr_pipeline.identity import TrustedMergeIdentity
            # ``merge_actor.py`` is package-only in the Mini deployment; the
            # flat autonomous entrypoint is deliberately its only caller.
            from pr_pipeline.merge_actor import MergeActor

        identity = TrustedMergeIdentity.from_record(raw_identity)
        actor = MergeActor(object(), lambda *_args: False)
        result = actor.merge(identity, execute=False)
    except Exception as exc:
        return f"fenced MergeActor shadow plan failed: {exc!r}"
    if result.status != "shadow":
        return "fenced MergeActor refused the shadow plan"
    return ""


def _merge_readiness(repo, pr, verdict, allowlist):
    """All merge gates EXCEPT shadow + tier autonomy — the shared source of truth
    for 'is this PR actually ready to merge?'. Returns (action, detail), action in
    {'merge','skip','blocked'}. evaluate() calls this after the shadow+tier gates;
    sweep_human_merge() calls it directly to find PRs that WOULD merge but for the
    tier gate (→ escalate for a human merge instead of looping forever). Network
    only via _pr_state (read-only)."""
    tier = verdict.get("tier", "high")
    if repo not in allowlist:
        return "skip", f"repo not on allowlist ({ALLOWLIST_PATH})"
    stored_head = verdict.get("head_sha") or ""
    info, err = _pr_state(repo, pr)
    if err:
        return "skip", err
    if (info["state"] or "").upper() != "OPEN":
        return "skip", f"PR state is {info['state']} (not OPEN)"
    # Honor explicit human holds (Caretaker fix, 2026-06-21): a DRAFT PR or one
    # carrying a hold label is NOT a merge candidate, even with a fresh PASS. The
    # validator may run and record a verdict on these, but the merge sweep must
    # leave them for the human (e.g. the hard-gate piece #1 held for Colin's
    # live sign-off). Fail-safe: skip (never merge) when held.
    if info.get("draft"):
        return "skip", "PR is a draft (held — author not ready to merge)"
    _held = [l for l in (info.get("labels") or [])
             if l.lower() in ("hold-for-colin", "do-not-merge", "do-not-auto-merge", "hold")]
    if _held:
        return "skip", f"PR carries hold label(s): {', '.join(_held)}"
    if not stored_head:
        return "blocked", "verdict has no head_sha — cannot confirm it matches live head"
    if info["head"] != stored_head:
        return "blocked", (f"PASS was for {stored_head[:8]} but live head is "
                           f"{info['head'][:8]}; re-validate")
    ok, why = validator_verdict.is_pass_fresh(repo, pr, info["head"])
    if not ok:
        return "skip", why
    # GitHub-native hard gates (honor branch protection if any exists).
    if (info["mergeable"] or "").upper() == "CONFLICTING" or info["merge_state"] == "DIRTY":
        return "blocked", "PR has merge conflicts"
    if info["merge_state"] == "BLOCKED":
        return "blocked", "branch protection BLOCKED (required check failing or review required)"
    # GATING checks only — monitoring/deploy smokes were filtered in _pr_state.
    if info["failing"]:
        return "blocked", f"gating checks failing: {', '.join(info['failing'][:5])}"
    if info["pending"]:
        return "skip", f"gating checks still pending: {', '.join(info['pending'][:5])}"
    # Fail CLOSED: never auto-merge a PR with NO green gating check. A repo whose
    # PRs run only non-gating checks (Vercel/preview/smoke) would otherwise leave
    # failing+pending empty and merge UNVERIFIED — violating the "CI green"
    # guardrail. Require at least one completed-SUCCESS gating check before merging.
    if not info.get("gating_green"):
        return "blocked", (
            "no green gating CI check on PR head — refusing to merge unverified "
            "(fail-closed; configure a code-gate workflow on pull_request). "
            f"non-gating checks seen: {', '.join(info['ignored'][:5]) or 'none'}"
        )
    # FAIL-CLOSED pre-merge tripwire re-check (behavior #3): the verdict says
    # PASS, but re-run the deterministic gate against the exact head we are about
    # to merge. A HIGH tripwire here means the stored PASS is wrong (overwrote a
    # BLOCK, was stale, or tripwires regressed) — refuse + alarm.
    tripped, highs, tw_note = _head_trips_tripwire(repo, pr)
    if tripped:
        _emit_tripwire_alarm(repo, pr, info["head"], highs)
        return "blocked", ("pre-merge tripwire HIGH on head "
                           f"{info['head'][:8]}: "
                           + "; ".join(f"{f.get('check')}" for f in highs[:5]))
    extra = f" (ignored non-gating red: {', '.join(info['ignored'][:3])})" if info["ignored"] else ""
    if tw_note:
        extra += f" [{tw_note}]"
    return "merge", (f"PASS tier={tier} head={stored_head[:8]} mergeable={info['mergeable']} "
                     f"gate={info['gating_green'][0]}{extra}")


# --- pre-merge tripwire re-check + alarm (Caretaker fix, 2026-06-21, behavior #3)
#
# WHY: even with a fresh PASS verdict, re-run the DETERMINISTIC tripwires against
# the exact head we are about to merge. If that head trips a HIGH tripwire the
# merge MUST NOT proceed (fail-closed) AND we emit a HIGH-VISIBILITY alarm the
# same way other Hermes alarms surface (print to stdout — the sweep cron delivers
# stdout to Slack, identical to hermes_usage_alert.py; no new channel invented).
# This is the post-merge-alarm requirement satisfied as a pre-merge BLOCK+WARN
# (the task's sanctioned "at minimum" fallback). Reversibility: delete this
# function and the call in evaluate().


def _head_trips_tripwire(repo, pr):
    """Return (tripped: bool, high_findings: list, note: str). Best-effort and
    fail-OPEN-on-infra-error (a gh/parse failure must not wedge the whole sweep);
    but if tripwires actually RUN and report HIGH findings, that is authoritative
    and the caller blocks. Network: read-only diff fetch."""
    try:
        if __package__:
            from . import validator_common as vc
            from . import validate_tripwires as vt
        else:
            import validator_common as vc
            import validate_tripwires as vt
        diff = vc.fetch_pr_diff(repo, pr)
        if not diff.strip():
            return False, [], "empty/failed diff — skipped pre-merge tripwire"
        tw = vt.run(diff, repo=repo, pr=pr)
        highs = [f for f in tw.get("findings", []) if f.get("severity") == "high"]
        return (bool(highs), highs, "")
    except Exception as e:
        return False, [], f"pre-merge tripwire skipped (infra): {e!r}"


def _emit_tripwire_alarm(repo, pr, head, highs):
    """HIGH-visibility alarm, same surface as hermes_usage_alert.py (stdout ->
    cron -> Slack). Logs to stderr too for the agent/gateway logs."""
    checks = "; ".join(f"{f.get('check')}:{f.get('file')}" for f in highs[:5])
    msg = (
        "🛑 *Hermes autonomous-merge BLOCKED a tripwire-failing head.*\n"
        f"`{repo}#{pr}` head `{(head or '')[:8]}` carried a fresh PASS verdict but "
        f"still trips {len(highs)} HIGH deterministic tripwire(s):\n"
        f"  • {checks}\n"
        "Merge refused (fail-closed). Re-validate — the PASS verdict disagrees "
        "with the deterministic gate (same class as jdmbuysell-v4#390)."
    )
    print(msg)                       # -> Slack via the merge sweep cron
    print(msg, file=sys.stderr)      # -> agent/gateway logs


def _do_merge(repo, pr):
    """Merge via the sanctioned ops path (re-enforces allowlist+verdict+shadow)."""
    try:
        r = subprocess.run(
            [PY, VAL_OPS, "merge-pr", repo, str(pr), "--squash"],
            capture_output=True, text=True, timeout=180, env=_shim_env())
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return r.returncode == 0, (out + (("\n" + err) if err else ""))[:400]
    except Exception as e:
        return False, f"merge subprocess error: {e!r}"


def _clickup_followup(task_id, repo, pr):
    """Best-effort: drop needs-validation, add merged tag, post a comment. Never
    fatal — the merge already happened.

    POLICY (2026-06-22): this post-PASS/merge writeback must NEVER set the ClickUp
    status to `complete`. Hermes's terminal ClickUp status is ALWAYS `in review`;
    `complete` is owned exclusively by the external QA gate (`ignite-validate` /
    Colin). On PASS the daemon may merge + deploy, but it leaves the task at
    `in review` — it does NOT touch status here. Do not add a status flip to
    `complete` to this function."""
    if not task_id:
        return
    def _ops(*args):
        try:
            subprocess.run([PY, VAL_OPS, *args], capture_output=True,
                           text=True, timeout=60, env=_shim_env())
        except Exception:
            pass
    _ops("rm-tag", task_id, "needs-validation")
    _ops("add-tag", task_id, "merged")
    body = f"/tmp/automerge_{task_id}.txt"
    try:
        with open(body, "w", encoding="utf-8") as f:
            f.write(f"Autonomous merge: {repo}#{pr} merged to main (squash) on a fresh "
                    f"validator PASS verdict. needs-validation tag cleared.")
        _post_clickup_comment(task_id, f"Autonomous merge: {repo}#{pr} merged to main (squash) on a fresh validator PASS verdict. needs-validation tag cleared.")
    except Exception:
        pass


def sweep(dry_run=False, only_repo=None, only_pr=None):
    """Sweep the verdict store and merge everything the gates permit.
    Returns a list of {repo, pr, action, detail}. Never raises."""
    results = []
    try:
        store = validator_verdict.load_verdicts()
        allowlist = _load_allowlist()
        for key, verdict in store.items():
            try:
                if "#" not in key:
                    continue
                repo, prs = key.rsplit("#", 1)
                pr = int(prs)
                if only_repo and repo != only_repo:
                    continue
                if only_pr and pr != only_pr:
                    continue
                if verdict.get("verdict") != "PASS":
                    # cheap pre-filter: only PASS verdicts are merge candidates
                    continue
                action, detail = evaluate(repo, pr, verdict, allowlist)
                rec = {"repo": repo, "pr": pr, "action": action, "detail": detail}
                if action == "merge" and not dry_run:
                    ok, msg = _do_merge(repo, pr)
                    rec["merge_ok"] = ok
                    rec["merge_out"] = msg
                    if ok:
                        _clickup_followup(verdict.get("task_id"), repo, pr)
                results.append(rec)
            except Exception as e:
                results.append({"repo": key, "pr": None, "action": "error",
                                "detail": f"{e!r}"})
    except Exception as e:
        results.append({"repo": "*", "pr": None, "action": "error",
                        "detail": f"sweep fatal: {e!r}"})
    return results


def sweep_gaps(allowlist=None):
    """Scan BLOCK verdicts to find PRs where the live head has moved since the
    BLOCK was written — these indicate a fix was pushed but re-validation was
    never triggered (a silent backlog gap). Called alongside sweep() so the
    merge-sweep cron surfaces hidden stale-block PRs in its Slack output.

    Returns list of {repo, pr, task_id, action='needs-revalidation', detail}.
    Read-only, never raises. Allowlist-scoped (same as sweep).
    """
    if allowlist is None:
        allowlist = _load_allowlist()
    results = []
    try:
        store = validator_verdict.load_verdicts()
        for key, verdict in store.items():
            try:
                if "#" not in key or verdict.get("verdict") != "BLOCK":
                    continue
                repo, prs = key.rsplit("#", 1)
                pr = int(prs)
                if repo not in allowlist:
                    continue
                stored_head = verdict.get("head_sha") or ""
                if not stored_head:
                    continue
                data, err = _gh_json(repo, pr, "state,headRefOid")
                if err:
                    continue
                if (data.get("state") or "").upper() != "OPEN":
                    continue
                live_head = data.get("headRefOid") or ""
                if live_head and live_head != stored_head:
                    results.append({
                        "repo": repo, "pr": pr,
                        "task_id": verdict.get("task_id") or "",
                        "action": "needs-revalidation",
                        "detail": (
                            f"BLOCK was for head {stored_head[:8]} but live head is "
                            f"{live_head[:8]} — new commits pushed, re-validate"
                        ),
                    })
            except Exception as e:
                results.append({"repo": key, "pr": None, "action": "error",
                                 "detail": f"gap-scan: {e!r}"})
    except Exception as e:
        results.append({"repo": "*", "pr": None, "action": "error",
                         "detail": f"gap-scan fatal: {e!r}"})
    return results


# --- human-merge escalation (Caretaker fix, 2026-07-02) ------------------------
#
# WHY: a PR that PASSes validation and is green + mergeable but whose risk tier is
# NOT in the autonomy allowlist (e.g. a new page + component = correctly not
# low-risk) falls into evaluate()'s "tier autonomy not enabled" skip and is then
# SILENT — nobody is told a human must merge it. The validator meanwhile keeps
# failing it on "is it live" (it never will be, absent a merge it can't get), so
# the task loops through execute↔review forever ("auto-merge expectation loop",
# brain: 2026-07-02). This scan closes that gap: it finds the would-merge-but-for-
# -tier PRs and escalates ONCE (deduped on head) via the same stdout->Slack surface
# as the tripwire alarm, plus a best-effort ClickUp needs-human tag + comment.
# It NEVER merges and NEVER changes ClickUp status. Reversibility: delete this
# block + the two call sites in main().

_HM_STATE = os.path.expanduser("~/.hermes/state/human_merge_escalations.json")


def _hm_load():
    try:
        with open(_HM_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _hm_save(state):
    try:
        os.makedirs(os.path.dirname(_HM_STATE), exist_ok=True)
        with open(_HM_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _emit_human_merge(repo, pr, head, task_id):
    """One-time escalation: stdout (-> Slack via the sweep cron) + best-effort
    ClickUp needs-human tag & comment. Never sets status (policy: complete is the
    external QA gate's, not the daemon's)."""
    msg = (
        "🙋 *Hermes needs a HUMAN merge.*\n"
        f"`{repo}#{pr}` head `{(head or '')[:8]}` PASSed validation and is green + "
        "mergeable, but its risk tier is not auto-merge-eligible — it will NOT "
        "auto-merge and will loop through execute↔review until a human merges it.\n"
        f"  → Review & merge `{repo}#{pr}` (or add a hold label to silence)."
    )
    print(msg)                    # -> Slack via the merge sweep cron stdout
    print(msg, file=sys.stderr)   # -> agent/gateway logs
    if not task_id:
        return
    def _ops(*args):
        try:
            subprocess.run([PY, VAL_OPS, *args], capture_output=True,
                           text=True, timeout=60, env=_shim_env())
        except Exception:
            pass
    _ops("add-tag", task_id, "needs-human")
    body = f"/tmp/needs_human_merge_{task_id}.txt"
    try:
        with open(body, "w", encoding="utf-8") as f:
            f.write(
                f"⚠️ NEEDS HUMAN MERGE: {repo}#{pr} passed validation and is green + "
                f"mergeable, but its risk tier is not auto-merge-eligible. It will "
                f"not auto-merge — a human must review & merge it (or the reuse/scope "
                f"decision must be settled first). Escalated once by autonomous_merge.")
        _ops("comment", task_id, "--body", body)
    except Exception:
        pass


def sweep_human_merge(allowlist=None, dry_run=False):
    """Find PASS verdicts that WOULD merge but for the tier-autonomy gate, and
    escalate each ONCE (deduped on head) for a human merge. Read-only w.r.t. the
    PR; writes only the dedup state + best-effort ClickUp tag/comment. Never
    raises. Returns list of {repo, pr, task_id, action='needs-human-merge', detail}.
    """
    if _shadow():
        return []  # whole validator in shadow — different mode, don't escalate
    if allowlist is None:
        allowlist = _load_allowlist()
    results, state, changed = [], _hm_load(), False
    try:
        store = validator_verdict.load_verdicts()
        for key, verdict in store.items():
            try:
                if "#" not in key or verdict.get("verdict") != "PASS":
                    continue
                repo, prs = key.rsplit("#", 1)
                pr = int(prs)
                if repo not in allowlist:
                    continue
                tier = verdict.get("tier", "high")
                if _tier_autonomy_enabled(tier):
                    continue  # the normal merge sweep handles these
                action, detail = _merge_readiness(repo, pr, verdict, allowlist)
                if action != "merge":
                    continue  # not actually ready (conflicts/pending/stale/held)
                head = verdict.get("head_sha") or ""
                skey = f"{repo}#{pr}"
                if state.get(skey) == head and head:
                    continue  # already escalated for this exact head
                results.append({
                    "repo": repo, "pr": pr,
                    "task_id": verdict.get("task_id") or "",
                    "action": "needs-human-merge",
                    "detail": f"tier={tier} green+mergeable head={head[:8]} — human merge required",
                })
                if not dry_run:
                    _emit_human_merge(repo, pr, head, verdict.get("task_id"))
                    state[skey] = head
                    changed = True
            except Exception as e:
                results.append({"repo": key, "pr": None, "action": "error",
                                "detail": f"human-merge scan: {e!r}"})
    except Exception as e:
        results.append({"repo": "*", "pr": None, "action": "error",
                        "detail": f"human-merge scan fatal: {e!r}"})
    if changed and not dry_run:
        _hm_save(state)
    return results


def main():
    p = argparse.ArgumentParser(description="Autonomous PR merge sweeper.")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would merge; touch nothing")
    p.add_argument("--repo", help="restrict to one owner/repo")
    p.add_argument("--pr", type=int, help="restrict to one PR number")
    a = p.parse_args()
    res = sweep(dry_run=a.dry_run, only_repo=a.repo, only_pr=a.pr)
    merged = [r for r in res if r.get("action") == "merge" and r.get("merge_ok")]
    would = [r for r in res if r.get("action") == "merge" and a.dry_run]
    for r in res:
        tag = r["action"].upper()
        if r["action"] == "merge":
            tag = "WOULD-MERGE" if a.dry_run else ("MERGED" if r.get("merge_ok") else "MERGE-FAILED")
        print(f"[automerge] {tag:12} {r['repo']}#{r['pr']} — {r['detail']}")
        if r.get("merge_out") and not r.get("merge_ok"):
            print(f"             {r['merge_out']}")
    # Gap scan: surface BLOCK verdicts where the PR head has moved since the
    # block was written. These are hidden backlog gaps (fix pushed, no re-validate).
    gaps = sweep_gaps()
    for g in gaps:
        tag = g["action"].upper()
        print(f"[automerge] {tag:12} {g['repo']}#{g['pr']} — {g['detail']}")
        if g.get("task_id"):
            print(f"             ClickUp task {g['task_id']} needs needs-validation re-tag")
    # Human-merge escalation: green+mergeable PASS PRs whose tier isn't auto-merge
    # eligible would otherwise loop forever. Surface + escalate each once.
    human = sweep_human_merge(dry_run=a.dry_run)
    for h in human:
        tag = h["action"].upper()
        print(f"[automerge] {tag:16} {h['repo']}#{h['pr']} — {h['detail']}")
        if h.get("task_id"):
            print(f"             ClickUp task {h['task_id']} tagged needs-human")
    print(json.dumps({"merged": len(merged), "would_merge": len(would),
                      "total": len(res), "gap_needs_revalidation": len(gaps),
                      "needs_human_merge": len([h for h in human if h.get('action') == 'needs-human-merge'])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
