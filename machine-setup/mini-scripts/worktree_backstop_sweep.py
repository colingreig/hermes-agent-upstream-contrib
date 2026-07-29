#!/usr/bin/env python3
"""
worktree_backstop_sweep.py — REDUNDANT, age-based safety-net for per-task worktrees.

WHY THIS EXISTS (2026-07-03, fixing the ~/dev/ignite-<taskId> clutter incident):
`cleanup_hermes_state.py` (launchd job `com.colingreig.hermes.cleanup-state`) is the PRIMARY
cleanup: it prunes a task's worktree/branch once its PR merges/closes and it has no active
claim. That is a merge-state-driven cleanup and can miss a worktree for many reasons — a PR
that's never opened, a task abandoned mid-flight, a claim file that outlives its task, a
worktree whose branch got deleted out of band, etc.

This script is the SEPARATE, dumber, age-based backstop the primary cleanup doesn't replace:
sweep anything matching the per-task worktree naming convention that is simply OLD, regardless
of merge/claim state, as a last-resort safety net. It is intentionally conservative (same
safety rules as the primary): never touches a dirty tree (uncommitted or unpushed work), never
touches anything with a `deliverable/` draft, and only ever touches paths matching the
`ignite-<taskId>` naming convention *inside its own scoped root* — never `~/dev` and never any
canonical repo checkout.

SCOPE (important — 2026-07-03 decision): this backstop is scoped to `~/.hermes/worktrees/`
ONLY, the new dedicated home for per-task worktrees (see
~/.hermes/skills/clickup-queue-poller/references/worktree-setup-pitfall.md). It deliberately
does NOT touch the pre-existing `~/dev/ignite-<taskId>` backlog from before this fix — that
cleanup is being handled separately, by hand, and this script must never race or interfere
with it. Once the legacy ~/dev backlog is gone, ~/dev should have zero `ignite-<taskId>`-shaped
dirs ever again (new work lands in ~/.hermes/worktrees/), so scoping here is also just "where
the real work happens" going forward.

Safety rules (conservative by design):
  - Only considers directories directly under the scoped root whose name matches
    `ignite-<taskId>` (alnum task id, i.e. matches ClickUp id shape) - never touches anything
    else, even if it looks unusual.
  - Skips (never removes) any candidate containing a `deliverable/` subdir (park-for-review
    drafts — see attach_deliverable.py).
  - Skips any candidate that is a git repo with commits ahead of `origin/HEAD` (unpushed work)
    OR with uncommitted changes (dirty worktree).
  - Age gate: only removes dirs whose mtime is older than --days (default 7, per the standing
    Hermes worktree-hygiene policy).
  - --dry-run lists candidates without removing anything.
  - Idempotent: safe to run repeatedly / on a schedule; a clean sweep with nothing to do is a
    normal, silent outcome.

Usage:
  python3 worktree_backstop_sweep.py [--dry-run] [--days 7] [--root ~/.hermes/worktrees]
  python3 worktree_backstop_sweep.py --write-retire-template /tmp/worktree-triage.json

Land-or-retire checkpoint:
  The regular scheduled run also consumes an explicit retirement manifest (default
  ~/.hermes/state/worktree-retire-approved.json). Generate a fingerprint-pinned review
  template with --write-retire-template, investigate each candidate, and change only
  human-approved entries to decision="retire" with classification, reason, and approved_at.
  The scheduled run fails closed unless the candidate is byte-for-byte unchanged.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Resolve the shared safety module under launchd's minimal env: __file__'s own directory
# first (normal case), then the conventional ~/.hermes/scripts location as a fallback in
# case this script is ever invoked via a symlink or copy elsewhere.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
# insert(0) reverses iteration order, so add the fallback first and the sibling
# directory second to keep the documented sibling-first import contract.
for _p in (_DEFAULT_SCRIPTS_DIR, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# worktree_safety.py (2026-07-10 refactor) is now the single source of truth for the
# deletion-safety predicates. This is a HARD dependency for this script: without it we
# cannot evaluate any candidate safely, so main() refuses to run (fails closed) if the
# import fails — see the _safety is None check below.
try:
    import worktree_safety as _safety
except Exception as _exc:  # pragma: no cover - see main()'s hard-refusal check
    _safety = None
    _WORKTREE_SAFETY_IMPORT_ERROR = _exc
else:
    _WORKTREE_SAFETY_IMPORT_ERROR = None

# Local aliases so the rest of this file's call sites (`_is_dirty(...)`, etc.) are
# unchanged by the refactor. All real logic lives in worktree_safety.py now.
_git = _safety._git if _safety else None
_is_dirty = _safety.is_dirty if _safety else None
_has_origin_remote = _safety.has_origin_remote if _safety else None
_default_ref = _safety.default_ref if _safety else None
_commits_ahead = _safety.commits_ahead if _safety else None
_content_landed = _safety.content_landed if _safety else None
AHEAD_UNKNOWN = _safety.AHEAD_UNKNOWN if _safety else None
HAS_WRITE_TREE = _safety.HAS_WRITE_TREE if _safety else False

TASK_DIR_RE = re.compile(r"^ignite-[A-Za-z0-9]+$")
RETIRE_MANIFEST_VERSION = 1


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"[{ts}] {msg}", flush=True)


def _run_git_bytes(args, cwd, timeout=30):
    """Run git without decoding its output so status fingerprints are lossless."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None


def _candidate_snapshot(root: Path, name: str):
    """Return a fail-closed identity snapshot for an approval-manifest candidate.

    The fingerprint deliberately includes HEAD, the complete porcelain status, and the
    git common-dir. A scheduled run may retire an explicitly approved tree only when all
    three still match the human-reviewed snapshot byte-for-byte. Broken symlinks use the
    link target as their identity. Remote URLs are never read or persisted here.
    """
    if not TASK_DIR_RE.match(name):
        return None
    wdir = root / name
    payload = {"name": name}

    if wdir.is_symlink():
        try:
            target = os.readlink(wdir)
        except OSError:
            return None
        payload.update({"kind": "symlink", "link_target": target})
    elif not wdir.is_dir():
        return None
    else:
        git_dir = wdir / ".git"
        if not git_dir.exists():
            return None
        head = _run_git_bytes(["rev-parse", "HEAD"], wdir)
        status = _run_git_bytes(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"], wdir
        )
        common = _run_git_bytes(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], wdir
        )
        if any(
            proc is None or proc.returncode != 0
            for proc in (head, status, common)
        ):
            return None
        payload.update(
            {
                "kind": "worktree" if git_dir.is_file() else "clone",
                "head": head.stdout.decode("utf-8", "surrogateescape").strip(),
                "status_sha256": hashlib.sha256(status.stdout).hexdigest(),
                "common_dir": common.stdout.decode(
                    "utf-8", "surrogateescape"
                ).strip(),
            }
        )

    fingerprint_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8", "surrogateescape")
    return {
        **payload,
        "fingerprint": hashlib.sha256(fingerprint_payload).hexdigest(),
    }


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_retire_template(root: Path, output_path: Path) -> int:
    entries = []
    for name in sorted(os.listdir(root)):
        snapshot = _candidate_snapshot(root, name)
        if snapshot is None:
            continue
        entries.append(
            {
                **snapshot,
                "decision": "review",
                "classification": "",
                "reason": "",
                "approved_at": "",
            }
        )
    _write_json_atomic(
        output_path,
        {"version": RETIRE_MANIFEST_VERSION, "entries": entries},
    )
    _log(f"TRIAGE_TEMPLATE_WRITTEN: {output_path} entries={len(entries)}")
    return 0


def _protected_common_dir(common_dir: str) -> bool:
    """Protect production and canonical developer checkouts from queue retirement."""
    if not common_dir:
        return True
    common = Path(common_dir).resolve()
    home = Path.home().resolve()
    if common == (home / ".hermes" / "hermes-agent" / ".git").resolve():
        return True
    dev = (home / "dev").resolve()
    try:
        common.relative_to(dev)
    except ValueError:
        return False
    return common.name == ".git"


def _process_retire_manifest(root: Path, manifest_path: Path, dry_run: bool):
    """Consume fingerprint-pinned, explicitly approved retirement decisions.

    This is the land-or-retire checkpoint between the read-only triage report and the
    regular scheduled sweep. It intentionally permits an approved dirty clone/worktree
    to be removed only when its entire status fingerprint is unchanged since approval.
    Any drift, claim, deliverable, malformed entry, protected common-dir, or git error
    fails closed and leaves both the tree and manifest entry untouched.
    """
    if not manifest_path.exists():
        return 0, 0, set()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"RETIRE_MANIFEST_REFUSED: unreadable {manifest_path}: {exc}")
        return 0, 1, set()
    if data.get("version") != RETIRE_MANIFEST_VERSION or not isinstance(
        data.get("entries"), list
    ):
        _log(f"RETIRE_MANIFEST_REFUSED: unsupported schema in {manifest_path}")
        return 0, 1, set()

    removed = 0
    blocked = 0
    reserved = set()
    allowed_classes = {"LANDED", "ABANDONED", "BROKEN_SYMLINK"}

    for entry in data["entries"]:
        if not isinstance(entry, dict) or entry.get("decision") != "retire":
            continue
        name = entry.get("name", "")
        if not TASK_DIR_RE.match(name):
            blocked += 1
            _log(f"RETIRE_BLOCKED: invalid-name {name!r}")
            continue
        reserved.add(name)
        if (
            entry.get("classification") not in allowed_classes
            or not entry.get("reason")
            or not entry.get("approved_at")
            or not entry.get("fingerprint")
        ):
            blocked += 1
            _log(f"RETIRE_BLOCKED: {name} (approval metadata incomplete)")
            continue

        snapshot = _candidate_snapshot(root, name)
        if snapshot is None:
            blocked += 1
            _log(f"RETIRE_BLOCKED: {name} (candidate missing or unreadable)")
            continue
        if snapshot["fingerprint"] != entry["fingerprint"]:
            blocked += 1
            _log(f"RETIRE_BLOCKED_DRIFT: {name} (snapshot changed after approval)")
            continue
        if snapshot.get("common_dir") and _protected_common_dir(
            snapshot["common_dir"]
        ):
            blocked += 1
            _log(f"RETIRE_BLOCKED_PROTECTED_COMMON_DIR: {name}")
            continue

        wdir = root / name
        if (wdir / "deliverable").is_dir():
            blocked += 1
            _log(f"RETIRE_BLOCKED_DELIVERABLE: {name}")
            continue
        if _is_claimed is not None:
            try:
                if _is_claimed(name[len("ignite-") :]):
                    blocked += 1
                    _log(f"RETIRE_BLOCKED_CLAIMED: {name}")
                    continue
            except Exception as exc:
                blocked += 1
                _log(f"RETIRE_BLOCKED_CLAIM_ERROR: {name}: {exc}")
                continue

        if dry_run:
            _log(
                f"WOULD_RETIRE_APPROVED: {name} "
                f"classification={entry['classification']}"
            )
            continue

        try:
            if snapshot["kind"] == "symlink":
                wdir.unlink()
            elif snapshot["kind"] == "worktree":
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(wdir),
                        "worktree",
                        "remove",
                        "--force",
                        str(wdir),
                    ],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0:
                    blocked += 1
                    _log(
                        f"RETIRE_BLOCKED_REMOVE_ERROR: {name}: "
                        f"{proc.stderr.strip()[:160]}"
                    )
                    continue
            elif snapshot["kind"] == "clone":
                shutil.rmtree(wdir)
            else:
                blocked += 1
                _log(f"RETIRE_BLOCKED_KIND: {name} ({snapshot['kind']})")
                continue
        except Exception as exc:
            blocked += 1
            _log(f"RETIRE_BLOCKED_REMOVE_ERROR: {name}: {exc}")
            continue

        entry["decision"] = "completed"
        entry["completed_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        entry["result"] = "removed"
        removed += 1
        # Persist each completed decision immediately. A long retirement batch can take
        # minutes; a process interruption must not leave already-removed entries marked
        # pending or lose their audit record until the very end of the run.
        _write_json_atomic(manifest_path, data)
        _log(
            f"RETIRED_APPROVED: {name} classification={entry['classification']}"
        )

    return removed, blocked, reserved


def _load_external_module(module_name, path):
    """Best-effort import of an optional external module by file path — same pattern
    cleanup_hermes_state.py uses for claim_store.py. Returns None on ANY failure so callers
    can degrade gracefully instead of hard-depending on it."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


# ClickUp-claim guard (2026-07-10, ADDITIONAL protection, never a hard dependency): if a
# task's worktree is claimed (an executor is actively working it), never let the backstop
# even consider it for removal, regardless of age. This is intentionally best-effort — if
# the claim subsystem is unavailable for any reason, this backstop must NOT block on it or
# treat unavailability as "everything is claimed" (that would freeze all pruning). It simply
# falls back to the pre-existing dirty/ahead/age gates, unchanged.
_claim_store = _load_external_module(
    "hermes_claim_store_backstop", os.path.join(_DEFAULT_SCRIPTS_DIR, "claim_store.py")
)
if _claim_store is not None:
    _is_claimed = _claim_store.is_claimed
else:
    _is_claimed = None
    _log("WARN: claim_store unavailable — backstop proceeding WITHOUT claim-awareness (dirty/ahead/age gates only)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--days", type=int, default=int(os.environ.get("HERMES_WORKTREE_BACKSTOP_DAYS", "7")),
                   help="age threshold in days (default 7)")
    p.add_argument("--root", default=os.environ.get("HERMES_WORKTREE_ROOT", "~/.hermes/worktrees"),
                   help="scoped root to sweep (default ~/.hermes/worktrees — NEVER point this at ~/dev)")
    p.add_argument(
        "--retire-manifest",
        default="~/.hermes/state/worktree-retire-approved.json",
        help="fingerprint-pinned approval manifest consumed before the age sweep",
    )
    p.add_argument(
        "--write-retire-template",
        metavar="PATH",
        help="write a read-only triage template for every candidate and exit",
    )
    args = p.parse_args(argv or sys.argv[1:])

    if _safety is None:
        _log(
            "REFUSING: worktree_safety module failed to import "
            f"({_WORKTREE_SAFETY_IMPORT_ERROR}) — cannot safely evaluate any candidate, "
            "aborting without touching anything"
        )
        return 2

    root = Path(os.path.expanduser(args.root)).resolve()

    # Hard safety fence: refuse to run against ~/dev or the user's home dir directly, no
    # matter what --root is passed — this backstop must never touch canonical repos.
    home = Path(os.path.expanduser("~")).resolve()
    forbidden = {home, home / "dev", home / "Projects"}
    if root in forbidden:
        _log(f"REFUSING: --root {root} is a protected canonical-repo location, not a worktree scratch root")
        return 2

    if not root.exists():
        _log(f"no-root-dir: {root}")
        return 0

    if args.write_retire_template:
        return _write_retire_template(
            root, Path(os.path.expanduser(args.write_retire_template))
        )

    retire_manifest = Path(os.path.expanduser(args.retire_manifest))
    approved_removed, approved_blocked, approved_names = _process_retire_manifest(
        root, retire_manifest, args.dry_run
    )

    now = time.time()
    removed = 0
    skipped_dirty = 0
    skipped_ahead = 0
    skipped_deliverable = 0
    skipped_recent = 0
    skipped_no_remote = 0
    skipped_claimed = 0
    landed_squash = 0

    for name in sorted(os.listdir(root)):
        if not TASK_DIR_RE.match(name):
            continue
        if name in approved_names:
            # Approved entries are owned exclusively by the fingerprint-pinned manifest
            # lane. A blocked/drifted entry must not fall through to the weaker age lane.
            continue
        wdir = root / name
        if not wdir.is_dir() or wdir.is_symlink():
            continue

        # ClickUp-claim guard: additional protection when the claim subsystem is
        # available, never a hard dependency (see the _is_claimed setup above). Placed
        # before the deliverable check so a claimed-but-undelivered worktree is still
        # protected for the right reason.
        if _is_claimed is not None:
            task_id = name[len("ignite-"):]
            try:
                claimed = bool(task_id and _is_claimed(task_id))
            except Exception as exc:
                claimed = False
                _log(f"WARN: claim check errored for {name}: {exc} — proceeding without claim protection for this candidate")
            if claimed:
                skipped_claimed += 1
                _log(f"SKIP_CLAIMED: {name} (live ClickUp claim for {task_id})")
                continue

        if (wdir / "deliverable").is_dir():
            skipped_deliverable += 1
            _log(f"SKIP_DELIVERABLE: {name}")
            continue

        git_dir = wdir / ".git"
        if git_dir.exists():
            # Note: this branch covers BOTH shapes — a linked worktree (`.git` is a FILE)
            # and a standalone legacy clone (`.git` is a DIRECTORY, e.g. ignite-86e251a3e).
            # Deliberately no shape-based shortcut here: a standalone clone gets exactly the
            # same remote + merged verification as a linked worktree, and is protected on
            # any ambiguity just the same.
            if _is_dirty(wdir):
                skipped_dirty += 1
                _log(f"SKIP_DIRTY: {name}")
                continue

            # FAIL-CLOSED GATE (2026-07-10): deletion eligibility requires an affirmatively
            # verified origin remote AND a resolvable default ref. No remote, or a remote
            # whose origin/HEAD/main/master can't be resolved, means we can never prove the
            # content is safely pushed/landed elsewhere. This is what protects unpushed,
            # no-remote clones like ignite-86e251a3e — never let this silently pass.
            if not _has_origin_remote(wdir):
                skipped_no_remote += 1
                _log(f"SKIP_NO_REMOTE: {name} (no origin remote configured)")
                continue
            if _default_ref(wdir) is None:
                skipped_no_remote += 1
                _log(f"SKIP_NO_REMOTE: {name} (origin/HEAD not resolvable)")
                continue

            ahead = _commits_ahead(wdir)
            if ahead is AHEAD_UNKNOWN:
                skipped_no_remote += 1
                _log(f"SKIP_NO_REMOTE: {name} (commits-ahead check failed — cannot verify, protecting)")
                continue
            if ahead > 0:
                if _content_landed(wdir):
                    landed_squash += 1
                    _log(f"LANDED_SQUASH: {name} | ahead={ahead} (content already on default branch, proceeding)")
                else:
                    skipped_ahead += 1
                    _log(f"SKIP_AHEAD_COMMITS: {name} | ahead={ahead}")
                    continue

        try:
            mtime = wdir.stat().st_mtime
        except OSError:
            mtime = now
        age_days = (now - mtime) / 86400
        if age_days < args.days:
            skipped_recent += 1
            _log(f"SKIP_RECENT: {name} | age_days={age_days:.1f}")
            continue

        # Linked worktrees (added via `git worktree add`) have .git as a FILE pointing
        # back at the bare mirror's worktree admin dir; legacy full clones have .git as
        # a directory. Both shapes can coexist under root during the migration.
        is_linked = git_dir.is_file()

        if args.dry_run:
            kind = "worktree" if is_linked else "clone"
            _log(f"WOULD_REMOVE {kind}: {name} | age_days={age_days:.1f}")
            continue

        # Final belt-and-suspenders check right before the destructive call. Dirty is always
        # a hard abort. No verifiable remote/default-ref is always a hard abort. An unknown
        # (failed) ahead check is always a hard abort. Ahead-of-origin is only an abort if
        # the content hasn't landed via a squash/rebase merge. Everything here is re-checked
        # fresh in case something changed mid-sweep — any error/ambiguity aborts, never falls
        # through to deletion.
        if git_dir.exists():
            if _is_dirty(wdir):
                _log(f"ABORT_LATE_CHANGE: {name} (dirty)")
                continue
            if not _has_origin_remote(wdir) or _default_ref(wdir) is None:
                _log(f"ABORT_LATE_CHANGE: {name} (no verifiable remote)")
                continue
            ahead_final = _commits_ahead(wdir)
            if ahead_final is AHEAD_UNKNOWN:
                _log(f"ABORT_LATE_CHANGE: {name} (commits-ahead check failed)")
                continue
            if ahead_final > 0 and not _content_landed(wdir):
                _log(f"ABORT_LATE_CHANGE: {name} (ahead={ahead_final}, not landed)")
                continue
        if not str(wdir).startswith(str(root) + os.sep):
            _log(f"FAILED_SAFETY_PATH_CHECK: {name}")
            continue
        try:
            if is_linked:
                # Remove via `git worktree remove` so the bare mirror's worktree admin
                # entry is cleaned up too (a bare rmtree would leave it dangling).
                rc = subprocess.run(
                    ["git", "-C", str(wdir), "worktree", "remove", "--force", str(wdir)],
                    capture_output=True, text=True,
                )
                if rc.returncode != 0:
                    # Fall back to rmtree if git couldn't resolve it (e.g. bare mirror
                    # already gone); the bare-prune pass below cleans up admin entries.
                    _log(f"WORKTREE_REMOVE_FALLBACK_RMTREE: {name}: {rc.stderr.strip()[:160]}")
                    shutil.rmtree(wdir)
                else:
                    _log(f"REMOVED_WORKTREE: {name} | age_days={age_days:.1f}")
            else:
                shutil.rmtree(wdir)
                _log(f"REMOVED_CLONE: {name} | age_days={age_days:.1f}")
            removed += 1
        except Exception as exc:
            _log(f"ERROR_REMOVING: {name} | {exc}")

    _log(
        f"sweep-finish root={root} removed={removed + approved_removed} "
        f"approved_removed={approved_removed} approved_blocked={approved_blocked} "
        f"skipped_dirty={skipped_dirty} "
        f"skipped_ahead={skipped_ahead} landed_squash={landed_squash} "
        f"skipped_deliverable={skipped_deliverable} "
        f"skipped_no_remote={skipped_no_remote} "
        f"skipped_claimed={skipped_claimed} "
        f"skipped_recent={skipped_recent} dry_run={args.dry_run}"
    )

    # Bare-mirror prune pass: clean up stale `git worktree` admin entries left behind
    # in each bare mirror under ~/.hermes/bare (e.g. from the rmtree fallback above, or
    # from worktrees removed out-of-band). Only runs for real (non-dry-run) invocations.
    bare_root = os.path.expanduser("~/.hermes/bare")
    if not args.dry_run and os.path.isdir(bare_root):
        for entry in sorted(os.listdir(bare_root)):
            bpath = os.path.join(bare_root, entry)
            if entry.endswith(".git") and os.path.isdir(bpath):
                subprocess.run(["git", "-C", bpath, "worktree", "prune"],
                               capture_output=True, text=True)
        _log("BARE_WORKTREE_PRUNE_DONE")

    return 0


if __name__ == "__main__":
    sys.exit(main())
