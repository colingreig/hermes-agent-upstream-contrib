#!/usr/bin/env python3
"""validate_pr_live_trigger.py — deterministic live trigger for the fenced PR validator.

WHY THIS EXISTS (task 86e2m44np): autonomous_merge.sweep() only ever merges a
PR that already has a fresh non-shadow PASS row in the SQLite verdict ledger
(~/.hermes/scripts/.validator_trust.sqlite3), but nothing on the Mini ever
invoked validate_pr.py — the only PR-linked agent job (hermes-pr-validate)
runs the generic ignite-validate skill, which never references this pipeline.
The ledger therefore had ZERO finalizations ever and autonomous merge was
structurally unreachable. This module is the deterministic producer that
closes that gap: it enumerates allowlisted-repo PRs that are fully CI-green
and unheld, and runs validate_pr.validate() on each so a genuine fenced
verdict is finalized.

TRUST BOUNDARY (unchanged, deliberately): a merge-eligible (non-shadow)
verdict can only be finalized by validate_pr.validate()'s own fenced review
flow while HERMES_VALIDATOR_FINALIZE_TOKEN is present in the process env
(validator_verdict.finalize_shadow_review). This trigger runs as its own
dedicated no_agent cron job whose jobs.json entry declares that token in
required_environment_variables, so the scheduler resolves it per-job
(profile/lazy-1P) and injects it ONLY into this child process — the token
stays out of the gateway boot environment (it is in
reconcile_launchd_environment.FORBIDDEN_BOOT_REFERENCE_KEYS) and executor
subprocess scrubbing keeps stripping it everywhere else. No executor or agent
gains any new way to mint a verdict: this process IS the validator's review
flow, and everything it finalizes goes through the same lease-fenced,
fail-closed validate_pr pipeline (tripwires, risk tier, panel, fail-closed
overrides).

CANDIDATE DISCIPLINE (fail-safe): verdicts are immutable per PR head, so this
trigger must never validate early. Only OPEN, non-draft, unheld PR heads with
zero failing/pending GATING checks and at least one green gating check are
ever handed to the validator — validating before CI completes would mint an
immutable BLOCK (via check_missing_ci) for a head that was about to go green.
Everything else is skipped and retried on a later tick. LLM spend is bounded
by a per-tick validation cap.
"""
from __future__ import annotations

import argparse
import bisect
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

if __package__:
    from . import autonomous_merge
    from . import identity
    from . import pr_pipeline_improvements
    from . import validate_pr
    from . import validator_repo_guard
    from . import validator_verdict
else:  # pragma: no cover - flat deployment fallback, mirrors validate_pr.py
    import autonomous_merge
    import identity
    import pr_pipeline_improvements
    import validate_pr
    import validator_repo_guard
    import validator_verdict

FINALIZE_TOKEN_ENV = "HERMES_VALIDATOR_FINALIZE_TOKEN"
MAX_VALIDATIONS_ENV = "HERMES_VALIDATOR_TRIGGER_MAX"
DEFAULT_MAX_VALIDATIONS = 3
PR_LIST_LIMIT = 30
GH_TIMEOUT = 60
STATE_PATH = Path.home() / ".hermes/state/validator-live-trigger.json"
# Mirrors autonomous_merge._merge_readiness — a held PR is not a candidate.
HOLD_LABELS = {"hold-for-colin", "do-not-merge", "do-not-auto-merge", "hold"}

# --- identity-failure cooldown (task: starvation fix, 2026-08-04) ----------
#
# WHY: candidates[:limit] always slices a deterministic prefix. A PR whose
# repo has no branch protection can NEVER validate — resolve_shadow_identity()
# raises VerdictStoreError every tick — so if such a PR sorts first it holds a
# slot forever and starves every other candidate. This section tracks
# per-candidate identity-resolution failures (NOT CI/content BLOCKs, which
# already produce a terminal verdict and drop out of scan_candidates on their
# own) and demotes repeat offenders to the back of the queue. Ordering and
# bookkeeping only — it never touches resolve_shadow_identity() or any trust
# check, and a failed/unreadable cooldown file always falls back to the
# original candidate order (fail-soft, never raises).
IDENTITY_FAILURES_FILENAME = ".validator_identity_failures.json"
IDENTITY_FAILURE_TTL_DAYS = 30


def _identity_failures_path() -> Path:
    """Directory mirrors validator_verdict.STORE_PATH (the TrustStore) so the
    cooldown bookkeeping file always lives beside the ledger it protects —
    reuses that existing HERMES_HOME resolution rather than hardcoding one."""
    return Path(validator_verdict.STORE_PATH).parent / IDENTITY_FAILURES_FILENAME


def _identity_failure_key(repo: str, pr: int) -> str:
    return f"{repo}#{pr}"


def _load_identity_failures(path=None) -> dict:
    """Fail-soft: any missing/corrupt/unreadable file yields {} — the caller
    then falls back to the original candidate ordering. Never raises."""
    p = Path(path) if path is not None else _identity_failures_path()
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _log_err(f"identity-failure cooldown unreadable, ignoring (non-fatal): {exc!r}")
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _log_err(f"identity-failure cooldown corrupt, ignoring (non-fatal): {exc!r}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: rec for key, rec in data.items()
        if isinstance(key, str) and isinstance(rec, dict)
    }


def _prune_identity_failures(data: dict, open_keys=None) -> dict:
    """Cap growth: drop TTL-expired entries and (when known) entries for PRs
    that are no longer open."""
    cutoff = time.time() - (IDENTITY_FAILURE_TTL_DAYS * 86400)
    pruned = {}
    for key, rec in data.items():
        if not isinstance(rec, dict):
            continue
        ts = rec.get("last_failure_ts")
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        if open_keys is not None and key not in open_keys:
            continue
        pruned[key] = rec
    return pruned


def _save_identity_failures(data: dict, path=None, open_keys=None) -> None:
    """Fail-soft: a write failure is logged and swallowed, never raised —
    losing cooldown bookkeeping must never block validation."""
    p = Path(path) if path is not None else _identity_failures_path()
    try:
        pruned = _prune_identity_failures(data, open_keys=open_keys)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(pruned, indent=2, sort_keys=True))
        tmp.replace(p)
    except Exception as exc:  # pragma: no cover - defensive, must never break the trigger
        _log_err(f"failed to persist identity-failure cooldown (non-fatal): {exc!r}")


def _reconcile_stale_heads(candidates: list, failures: dict) -> bool:
    """Clear a failure record whose stored head no longer matches the
    candidate's current head — a new push deserves a fresh attempt. Returns
    True iff the in-memory failures dict changed."""
    changed = False
    for item in candidates:
        key = _identity_failure_key(item["repo"], item["pr"])
        rec = failures.get(key)
        if isinstance(rec, dict) and rec.get("head") != item.get("head"):
            del failures[key]
            changed = True
    return changed


def _order_candidates(candidates: list, failures: dict) -> list:
    """Stable-sort so never-attempted candidates come first, then
    identity-failed candidates ordered by last_failure_ts ascending (longest
    since failure next). Relative order within each group is preserved
    (Python's sort is stable) — this only reorders, never filters."""
    if not failures:
        return list(candidates)

    def sort_key(item):
        rec = failures.get(_identity_failure_key(item["repo"], item["pr"]))
        if not isinstance(rec, dict):
            return (0, 0.0)
        ts = rec.get("last_failure_ts")
        if not isinstance(ts, (int, float)):
            return (0, 0.0)
        return (1, ts)

    return sorted(candidates, key=sort_key)


def _record_identity_failure(failures: dict, repo: str, pr: int, head: str) -> None:
    key = _identity_failure_key(repo, pr)
    existing = failures.get(key)
    count = 1
    if isinstance(existing, dict) and existing.get("head") == head:
        prior = existing.get("failure_count")
        if isinstance(prior, int) and prior > 0:
            count = prior + 1
    failures[key] = {"head": head, "last_failure_ts": time.time(), "failure_count": count}


def _clear_identity_failure(failures: dict, repo: str, pr: int) -> bool:
    return failures.pop(_identity_failure_key(repo, pr), None) is not None


def _log(msg: str) -> None:
    print(f"[validator-trigger] {msg}", flush=True)


def _log_err(msg: str) -> None:
    print(f"[validator-trigger] {msg}", file=sys.stderr, flush=True)


def finalize_token_present() -> bool:
    return bool((os.environ.get(FINALIZE_TOKEN_ENV, "") or "").strip())


def max_validations() -> int:
    raw = (os.environ.get(MAX_VALIDATIONS_ENV, "") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_VALIDATIONS
    return value if value > 0 else DEFAULT_MAX_VALIDATIONS


def _candidate_key(candidate) -> tuple[str, int, str]:
    """Stable identity used for deterministic ordering and the durable cursor."""
    return candidate["repo"], candidate["pr"], candidate["head"]


def _load_cursor() -> tuple[str, int, str] | None:
    """Load the last reservation, refusing malformed/unreadable state."""
    try:
        with STATE_PATH.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except FileNotFoundError:
        return None
    except Exception as exc:
        raise RuntimeError(f"round-robin state unreadable: {exc}") from exc

    key = state.get("last_reserved_candidate") if isinstance(state, dict) else None
    if not (
        isinstance(key, list)
        and len(key) == 3
        and isinstance(key[0], str)
        and isinstance(key[1], int)
        and not isinstance(key[1], bool)
        and isinstance(key[2], str)
    ):
        raise RuntimeError("round-robin state malformed")
    try:
        canonical_repo = identity.canonicalize_repo(key[0])
    except identity.IdentityError:
        raise RuntimeError("round-robin state malformed") from None
    if (
        key[0] != canonical_repo
        or key[1] <= 0
        or len(key[2]) != 40
        or any(character not in "0123456789abcdefABCDEF" for character in key[2])
    ):
        raise RuntimeError("round-robin state malformed")
    return key[0], key[1], key[2]


def _reserve_candidate(key: tuple[str, int, str]) -> None:
    """Atomically persist a reservation before its validation can begin."""
    path = STATE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump({"last_reserved_candidate": list(key)}, state_file,
                          sort_keys=True)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
    except Exception as exc:
        raise RuntimeError(f"round-robin reservation failed: {exc}") from exc


def _round_robin_candidates(candidates, cursor, deprioritized_keys=frozenset()):
    """Return candidates in stable order, beginning strictly after cursor.

    ``deprioritized_keys`` (identity-failure keys, i.e. ``"repo#pr"``) are
    stably pushed behind every other candidate afterwards — never dropped,
    just given last turn — so a currently-cooling-down PR cannot monopolize
    a bounded tick's slots ahead of candidates that haven't recently failed.
    """
    ordered = sorted(candidates, key=_candidate_key)
    if not ordered:
        return ordered
    if cursor is not None:
        keys = [_candidate_key(candidate) for candidate in ordered]
        start = bisect.bisect_right(keys, cursor)
        ordered = ordered[start:] + ordered[:start]
    if not deprioritized_keys:
        return ordered
    preferred, deprioritized = [], []
    for candidate in ordered:
        bucket = deprioritized if (
            _identity_failure_key(candidate["repo"], candidate["pr"]) in deprioritized_keys
        ) else preferred
        bucket.append(candidate)
    return preferred + deprioritized


@contextmanager
def _validation_run_lock():
    """Serialize live selection and validation across trigger processes."""
    lock_path = STATE_PATH.with_name(f"{STATE_PATH.name}.run.lock")
    lock_file = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except Exception as exc:
        if lock_file is not None:
            lock_file.close()
        raise RuntimeError(f"validation run lock failed: {exc}") from exc

    try:
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _select_and_reserve_candidates(candidates, limit, deprioritized_keys=frozenset()):
    """Select and reserve one batch as a cross-process atomic transaction.

    This shorter lock remains distinct from the live-run lock because direct
    callers also need atomic reservations. Normal live runs acquire the run lock
    first and this selection lock second, then release them in reverse order.
    """
    lock_path = STATE_PATH.with_name(f"{STATE_PATH.name}.lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                cursor = _load_cursor()
                selected = _round_robin_candidates(
                    candidates, cursor, deprioritized_keys=deprioritized_keys)[:limit]
                if selected:
                    _reserve_candidate(_candidate_key(selected[-1]))
                return selected
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"round-robin transaction failed: {exc}") from exc


def _list_open_prs(repo):
    """Return (prs, err). prs is a list of gh PR dicts; err set on fetch error."""
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--json", "number,title,body,headRefName",
             "--limit", str(PR_LIST_LIMIT)],
            capture_output=True, text=True, timeout=GH_TIMEOUT,
            env=autonomous_merge._shim_env())
    except Exception as exc:
        return None, f"gh pr list failed: {exc!r}"
    if r.returncode != 0:
        return None, f"gh pr list rc={r.returncode}: {(r.stderr or '').strip()[:160]}"
    try:
        prs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as exc:
        return None, f"gh pr list returned malformed JSON: {exc}"
    return prs if isinstance(prs, list) else [], None


def candidate_skip_reason(info) -> str:
    """Return '' when a PR head is safe to validate live, else the skip reason.

    FAIL-SAFE: only a fully CI-green, unheld, open head is ever validated —
    the verdict store is immutable per head, so validating a head whose gating
    CI is failing or still pending would permanently BLOCK that head (via
    adversarial_review.check_missing_ci) even if CI went green minutes later.
    Reuses autonomous_merge.pr_state()'s gating classification so the
    validate-time and merge-time views of "is CI green" can never drift.
    """
    if (info.get("state") or "").upper() != "OPEN":
        return f"PR state is {info.get('state')} (not OPEN)"
    if info.get("draft"):
        return "PR is a draft (held)"
    held = [l for l in (info.get("labels") or []) if l.lower() in HOLD_LABELS]
    if held:
        return f"hold label(s): {', '.join(held)}"
    if (info.get("mergeable") or "").upper() == "CONFLICTING" or info.get("merge_state") == "DIRTY":
        return "PR has merge conflicts"
    if info.get("failing"):
        return f"gating checks failing: {', '.join(info['failing'][:3])}"
    if info.get("pending"):
        return f"gating checks pending: {', '.join(info['pending'][:3])}"
    if not info.get("gating_green"):
        return "no green gating check on head (fail-safe: not validating unverified code)"
    return ""


def scan_candidates(allowlist):
    """Enumerate validation candidates across canonical allowlisted repositories.

    Returns (candidates, skipped, errors). A candidate is a dict with
    repo/pr/head/task_id. Every valid allowlist spelling is resolved before any
    PR lookup and replaced by its canonical owner/name. Canonical repositories
    must form a bijection between immutable node_id and normalized owner/name;
    aliases resolving to the same pair are scanned once, while either-direction
    identity conflicts fail closed before PR discovery. Equal PR/head pairs in
    repositories with distinct canonical identities remain independent.
    Repositories without a complete, consistent canonical identity fail closed
    before their PRs are scanned.
    """
    candidates, skipped, errors = [], [], []
    canonical_names_by_node = {}
    canonical_nodes_by_name = {}

    # Resolve the complete allowlist first. Keeping identity discovery separate
    # from PR discovery prevents an alias from leaking into any downstream API,
    # ledger, candidate, cursor, validation, or summary subject.
    for allowlisted_repo in sorted(allowlist):
        try:
            canonical = validator_repo_guard.canonical_identity(allowlisted_repo)
        except Exception as exc:
            errors.append(
                f"{allowlisted_repo}: canonical repository identity resolution failed: {exc!r}"
            )
            continue
        node_id = canonical.get("node_id") if isinstance(canonical, dict) else None
        full_name = canonical.get("full_name") if isinstance(canonical, dict) else None
        if not isinstance(full_name, str):
            errors.append(
                f"{allowlisted_repo}: canonical repository identity is unresolved or malformed"
            )
            continue
        try:
            canonical_full_name = identity.canonicalize_repo(full_name)
        except identity.IdentityError:
            errors.append(
                f"{allowlisted_repo}: canonical repository identity is unresolved or malformed"
            )
            continue
        if (
            not isinstance(node_id, str)
            or not node_id.strip()
            or node_id != node_id.strip()
        ):
            errors.append(
                f"{allowlisted_repo}: canonical repository identity is unresolved or malformed"
            )
            continue
        canonical_names_by_node.setdefault(node_id, set()).add(canonical_full_name)
        canonical_nodes_by_name.setdefault(canonical_full_name, set()).add(node_id)

    conflicting_nodes = {
        node_id for node_id, full_names in canonical_names_by_node.items()
        if len(full_names) != 1
    }
    conflicting_names = {
        full_name for full_name, node_ids in canonical_nodes_by_name.items()
        if len(node_ids) != 1
    }
    for node_id in sorted(conflicting_nodes):
        errors.append(
            f"canonical repository identity {node_id!r} has conflicting owner/name values: "
            f"{', '.join(sorted(canonical_names_by_node[node_id]))}"
        )
    for full_name in sorted(conflicting_names):
        errors.append(
            f"canonical repository identity {full_name!r} has conflicting node_id values: "
            f"{', '.join(sorted(canonical_nodes_by_name[full_name]))}"
        )

    canonical_repositories = []
    for node_id, full_names in canonical_names_by_node.items():
        if node_id in conflicting_nodes:
            continue
        full_name = next(iter(full_names))
        if full_name in conflicting_names:
            continue
        canonical_repositories.append((full_name, node_id))

    seen_heads = set()
    for repo, repository_key in sorted(canonical_repositories):
        prs, err = _list_open_prs(repo)
        if err:
            errors.append(f"{repo}: {err}")
            continue
        for pr_data in prs:
            number = pr_data.get("number")
            if not isinstance(number, int):
                continue
            info, perr = autonomous_merge.pr_state(repo, number)
            if perr:
                errors.append(f"{repo}#{number}: {perr}")
                continue
            head = info.get("head") or ""
            if not head:
                errors.append(f"{repo}#{number}: no head SHA")
                continue
            dedupe_key = (repository_key, number, head)
            if dedupe_key in seen_heads:
                continue
            seen_heads.add(dedupe_key)
            reason = candidate_skip_reason(info)
            if reason:
                skipped.append({"repo": repo, "pr": number, "reason": reason})
                continue
            try:
                existing = validator_verdict.verdict_for(repo, number, head_sha=head)
            except validator_verdict.VerdictStoreError as exc:
                errors.append(f"{repo}#{number}: verdict store unreadable: {exc}")
                continue
            if existing is not None:
                skipped.append({
                    "repo": repo, "pr": number,
                    "reason": f"immutable verdict already recorded for head {head[:8]} "
                              f"({existing.get('verdict')})",
                })
                continue
            task_id = (
                pr_pipeline_improvements._extract_clickup_task_id(pr_data.get("body"))
                or pr_pipeline_improvements._extract_clickup_task_id(pr_data.get("title"))
                or pr_pipeline_improvements._extract_clickup_task_id(pr_data.get("headRefName"))
            )
            candidates.append({
                "repo": repo, "pr": number, "head": head, "task_id": task_id,
            })
    return candidates, skipped, errors


def run(dry_run: bool = False, cap: int | None = None):
    """One trigger tick. Returns a JSON-serializable summary dict."""
    summary = {
        "dry_run": bool(dry_run),
        "candidates": [],
        "skipped": [],
        "errors": [],
        "validated": [],
    }
    if not finalize_token_present():
        # The cron job declares the token in required_environment_variables so
        # the scheduler fails the run before this script even starts when it
        # cannot be resolved — this in-process check is belt-and-suspenders.
        # Running without it would silently downgrade every verdict to shadow
        # (never merge-eligible) while spending real LLM panel money: refuse
        # loudly instead so the fleet-outcome contract can alarm on it.
        _log_err("FINALIZE TOKEN ABSENT — refusing to run live validations "
                 "(verdicts would be silently shadow-only)")
        summary["errors"].append("finalize-token-absent")
        return 1, summary

    allowlist = autonomous_merge._load_allowlist()
    if not allowlist:
        _log("allowlist is empty — nothing to validate")
        return 0, summary

    limit = cap if isinstance(cap, int) and cap > 0 else max_validations()
    failures, failures_dirty = {}, False
    try:
        # Dry runs remain strictly observational: nullcontext neither creates
        # nor acquires the adjacent live-run lock. Live runs acquire the full-run
        # lock before scanning so selection never consumes a snapshot captured
        # while a prior run was still validating.
        run_lock = nullcontext() if dry_run else _validation_run_lock()
        with run_lock:
            candidates, skipped, errors = scan_candidates(allowlist)
            summary["skipped"] = skipped
            summary["errors"].extend(errors)
            for item in skipped:
                _log(f"skip {item['repo']}#{item['pr']}: {item['reason']}")
            for err in errors:
                _log_err(f"scan error (non-fatal): {err}")

            # Identity-failure cooldown ordering (fail-soft: any problem here
            # falls back to scan_candidates()'s original order and never
            # raises). Runs inside the same run_lock as scan/reservation so
            # the ordering snapshot can never race a concurrent process.
            try:
                failures = _load_identity_failures()
                failures_dirty = _reconcile_stale_heads(candidates, failures)
                candidates = _order_candidates(candidates, failures)
            except Exception as exc:  # pragma: no cover - defensive, must never block validation
                _log_err(f"identity-failure cooldown ordering failed, using scan order "
                         f"(non-fatal): {exc!r}")
                failures, failures_dirty = {}, False

            summary["candidates"] = candidates
            # Round-robin selection re-sorts deterministically by candidate
            # identity (see _round_robin_candidates), which would otherwise
            # discard the fresh-first ordering above; threading the current
            # failure-key set through as deprioritized_keys keeps recently
            # identity-failed candidates from monopolizing a bounded tick's
            # slots ahead of never-attempted or long-recovered candidates.
            deprioritized_keys = frozenset(failures.keys())

            if dry_run:
                # A dry run observes the current cursor but must not create either
                # reservation state or either adjacent lock file.
                cursor = _load_cursor()
                to_validate = _round_robin_candidates(
                    candidates, cursor, deprioritized_keys=deprioritized_keys)[:limit]
            else:
                to_validate = _select_and_reserve_candidates(
                    candidates, limit, deprioritized_keys=deprioritized_keys)
            if len(candidates) > limit:
                _log(f"{len(candidates)} candidate(s); validating round-robin "
                     f"selected {limit} (cap, remainder next tick)")

            for item in to_validate:
                repo, pr, task_id = item["repo"], item["pr"], item["task_id"]
                if dry_run:
                    _log(f"DRY_RUN would validate {repo}#{pr} head={item['head'][:8]} "
                         f"task={task_id or '-'}")
                    continue
                _log(f"validating {repo}#{pr} head={item['head'][:8]} task={task_id or '-'}")
                try:
                    rc, result = validate_pr.validate(
                        repo, pr, task=task_id, shadow=False, allow_panel=True,
                        expected_repo=repo)
                except Exception as exc:
                    _log_err(f"{repo}#{pr} validation crashed (non-fatal): {exc!r}")
                    summary["errors"].append(f"{repo}#{pr}: {exc!r}")
                    continue
                outcome = {
                    "repo": repo, "pr": pr, "rc": rc,
                    "verdict": result.get("verdict"),
                    "tier": result.get("tier"),
                    "shadow": result.get("shadow"),
                }
                summary["validated"].append(outcome)
                _log(f"{repo}#{pr} -> {outcome['verdict']} tier={outcome['tier']} "
                     f"shadow={outcome['shadow']} rc={rc}")

                # Identity-failure bookkeeping (ordering-only; never affects rc/verdict).
                # rc==2 with fenced=True is VerdictBusyError — a transient lease
                # conflict, not an identity failure — and must not be recorded.
                try:
                    if rc == 2 and not result.get("fenced"):
                        _record_identity_failure(failures, repo, pr, item["head"])
                        failures_dirty = True
                    elif rc in (0, 1):
                        if _clear_identity_failure(failures, repo, pr):
                            failures_dirty = True
                except Exception as exc:  # pragma: no cover - defensive
                    _log_err(f"identity-failure cooldown bookkeeping failed (non-fatal): {exc!r}")
    except RuntimeError as exc:
        _log_err(str(exc))
        summary["errors"].append(str(exc))
        return 1, summary

    if failures_dirty and not dry_run:
        open_keys = {_identity_failure_key(c["repo"], c["pr"]) for c in candidates}
        open_keys |= {_identity_failure_key(s["repo"], s["pr"]) for s in skipped}
        _save_identity_failures(failures, open_keys=open_keys)

    return 0, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic live trigger for the fenced PR validator.")
    parser.add_argument("--dry-run", action="store_true",
                        help="enumerate candidates without validating")
    parser.add_argument("--max", type=int, default=0,
                        help=f"per-tick validation cap (default env "
                             f"{MAX_VALIDATIONS_ENV} or {DEFAULT_MAX_VALIDATIONS})")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or bool(os.environ.get("DRY_RUN"))
    rc, summary = run(dry_run=dry_run, cap=args.max if args.max > 0 else None)
    print(json.dumps({
        "trigger": "validator-live-trigger",
        "candidates": len(summary["candidates"]),
        "validated": len(summary["validated"]),
        "skipped": len(summary["skipped"]),
        "errors": summary["errors"],
        "results": summary["validated"],
    }, indent=2))
    return rc


def safe_main() -> int:
    """Never let an operational failure break cron delivery."""
    try:
        return main()
    except Exception as exc:  # pragma: no cover - defensive cron wrapper
        _log_err(f"unexpected trigger error: {exc!r}")
        print(json.dumps({"trigger": "validator-live-trigger", "error": repr(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(safe_main())
