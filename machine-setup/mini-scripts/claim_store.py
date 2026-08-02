#!/usr/bin/env python3
"""claim_store.py — cross-process per-task claim store for N>=2 executor concurrency.

Why this exists
---------------
The gate (`clickup_poll_gate.py`) already has a per-task `fcntl.flock` in
`_try_acquire_claim`, but that lock is held only WITHIN a single gate tick (one
process) and released as soon as TARGET_PATH is written. It serializes gate
wakes; it does NOT survive the executor's actual work.

An executor claim is different: the executor is an LLM agent that runs as MANY
separate subprocess tool-calls over up to ~90 min. A held flock dies the instant
the acquiring subprocess exits, so it cannot represent "executor X owns task T
for the duration of its run." We therefore represent a claim as a FILE whose
existence + holder-liveness + age IS the claim:

    ~/.hermes/state/claims/<taskId>.claim   ->  {"pid","host","ts","taskId","run"}

Acquisition is atomic via O_CREAT|O_EXCL (first writer wins). If the file already
exists we inspect it under a brief flock (to serialise the stale reclaim path)
and:
  * age < TTL   -> LIVE claim, refuse.
  * age >= TTL  -> stale (holder crashed without releasing), reclaim, acquire.

Liveness is TTL + heartbeat + explicit release, NOT process-liveness. PID is
deliberately unusable here: executor runs are THREADS inside the long-lived
gateway process, so every executor shares the gateway PID — os.kill can neither
prove a run is alive nor tell two concurrent executors apart. Recency (mtime
within TTL) is the only sound liveness signal, matching the gate's existing
90-min CLAIM_LOCK_TTL_S reclaim policy. A run longer than TTL must heartbeat()
to keep its claim; a crashed run is reclaimable after TTL. Release ownership is a
non-issue because each executor only ever touches ITS OWN task's claim file
(distinct task_id -> distinct file), so it can never release a peer's claim.

FAIL-OPEN-TO-ACQUIRE
--------------------
Any internal error (permission, disk, parse) returns "acquired". The claim
machinery must NEVER block a legitimate executor claim — at N=1 (uncontended)
this module is a pure no-op that always grants the claim, preserving today's
behaviour exactly. The ONLY thing that ever refuses is a provably-live competing
claim by another live process on this host.

CLI (used by the clickup-queue-poller skill at the claim step):
    python3 claim_store.py acquire   <taskId> [--run <id>]   # exit 0 acquired, 10 live-claimed
    python3 claim_store.py release   <taskId>                # exit 0
    python3 claim_store.py heartbeat <taskId>                # exit 0 (refresh mtime)
    python3 claim_store.py is-claimed <taskId>               # exit 0 claimed (live), 1 free
    python3 claim_store.py list                              # prints live claims as JSON
    python3 claim_store.py reap                              # unlink dead/stale claim files; prints reclaimed ids

Liveness is intentionally TTL + heartbeat + explicit release. Claiming happens
inside short-lived terminal subprocesses, so their PIDs cannot represent the
long-lived agent turn that owns the lease.
"""
from __future__ import annotations

import json
import importlib.util
import os
import socket
import sys
import time

try:
    import fcntl  # POSIX only; this host is macOS
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

CLAIMS_DIR = os.path.expanduser("~/.hermes/state/claims")
# TTL must exceed the longest executor run. The gate uses 90 min
# (CLAIM_LOCK_TTL_S); match it so the two mechanisms agree on staleness.
CLAIM_TTL_S = int(os.environ.get("HERMES_CLAIM_TTL_S", str(90 * 60)))
_HOST = socket.gethostname()


def _load_activity_journal():
    """Load the sibling deployed journal without making claims depend on it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_activity_journal.py")
    try:
        spec = importlib.util.spec_from_file_location("hermes_report_activity_journal", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


_activity_journal = _load_activity_journal()


def _report_degraded(reason: str) -> None:
    if _activity_journal is not None:
        _activity_journal.mark_degraded(reason, source="queue-poller.claim_store")
    else:
        sys.stderr.write(f"report activity health UNKNOWN: {reason}; journal unavailable\n")


def _report_durable_claim(task_id: str, run: str | None) -> None:
    if _activity_journal is None:
        _report_degraded("durable claim was not journaled because emitter is unavailable")
        return
    _activity_journal.safe_emit(
        kind="claim",
        task_id=task_id,
        source="queue-poller.claim_store",
        run_id=run or os.environ.get("HERMES_EXECUTOR_RUN_ID") or None,
    )


def _fsync_claim_dir() -> None:
    fd = os.open(CLAIMS_DIR, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _claim_path(task_id: str) -> str:
    os.makedirs(CLAIMS_DIR, exist_ok=True)
    # ".claim" (not ".lock") so it never collides with the gate's flock file,
    # which lives at <taskId>.lock. The two coordinate via is_claimed(), below.
    return os.path.join(CLAIMS_DIR, f"{task_id}.claim")


def _payload(task_id: str, run: str | None) -> bytes:
    return json.dumps({
        "pid": os.getpid(),
        "host": _HOST,
        "ts": time.time(),
        "taskId": task_id,
        "run": run or os.environ.get("HERMES_EXECUTOR_RUN_ID", ""),
    }).encode()


def _read(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    """True iff *pid* is a live process on this host. ``os.kill(pid, 0)`` is the
    POSIX liveness probe. PermissionError means the pid exists but is owned by
    another user (treat as alive — conservative); ProcessLookupError means it's
    gone."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True  # unknown -> assume alive (never false-reclaim)


def _holder_provably_dead(rec: "dict | None") -> bool:
    """True only when we can PROVE the claim's holder is dead: the claim was
    written ON THIS HOST and its recorded pid is no longer a live process.

    This is strictly additive to the TTL check and can never false-reclaim a
    live run: a live holder's pid passes ``_pid_alive`` (executors here run as
    distinct subprocesses with their own pid — the module docstring's
    "threads share the gateway pid" assumption does not hold in practice, and
    even if it did, a live gateway pid reads as alive). Pid reuse can only make
    a dead holder look alive, which merely defers reclaim to the TTL — never the
    reverse. Cross-host claims are left to the TTL (we can't probe another host).
    """
    try:
        if not rec or rec.get("host") != _HOST:
            return False
        pid = rec.get("pid")
        if isinstance(pid, int) and pid > 0:
            return not _pid_alive(pid)
    except Exception:
        pass
    return False


def _is_live(path: str) -> bool:
    """A claim is live while its heartbeat is within TTL.

    The acquiring terminal subprocess exits before the agent's next tool call,
    so PID liveness would immediately reclaim a valid cross-call lease.
    """
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return False
    return age < CLAIM_TTL_S


def acquire(task_id: str, run: str | None = None) -> bool:
    """Atomically acquire the claim for task_id.

    Returns True if acquired (incl. all fail-open paths), False ONLY when a
    provably-live competing claim exists.
    """
    if fcntl is None:
        _report_degraded("claim acquisition granted fail-open because fcntl is unavailable")
        return True  # non-POSIX: fail-open
    try:
        path = _claim_path(task_id)
    except Exception as exc:
        _report_degraded(f"claim acquisition granted fail-open before path creation: {exc}")
        return True
    try:
        # Fast path: atomic exclusive create. Winner of the create owns it.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            payload = _payload(task_id, run)
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError(f"short claim write: {written}/{len(payload)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_claim_dir()
        _report_durable_claim(task_id, run)
        return True
    except FileExistsError:
        pass
    except Exception as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        _report_degraded(f"claim acquisition granted fail-open after create failure: {exc}")
        return True  # FAIL-OPEN: never block a legit claim on a store bug
    # File exists — serialise the inspect/reclaim with a brief flock so two
    # processes can't both reclaim a stale lock.
    try:
        with open(path, "r+", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
            except OSError as exc:
                _report_degraded(f"claim acquisition granted fail-open after flock failure: {exc}")
                return True  # can't lock to inspect -> fail-open
            if _is_live(path):
                return False  # live claim by another process — refuse
            # Dead/stale holder: reclaim in place (truncate + rewrite).
            f.seek(0)
            f.truncate()
            f.write(_payload(task_id, run).decode())
            f.flush()
            os.fsync(f.fileno())
        os.utime(path, None)  # fresh mtime -> TTL window restarts now
        _fsync_claim_dir()
        _report_durable_claim(task_id, run)
        return True
    except Exception as exc:
        _report_degraded(f"claim acquisition granted fail-open after rewrite failure: {exc}")
        return True  # FAIL-OPEN


def release(task_id: str, run: str | None = None) -> None:
    """Release the claim. Safe to call even if not held. Never raises.

    If ``run`` is given, only unlink when the file's run matches — this guards
    the over-TTL edge: if this run exceeded TTL and a PEER already reclaimed the
    task, we must NOT delete the peer's fresh claim. Without a run (best-effort)
    we unlink unconditionally."""
    path = _claim_path(task_id)
    if run is not None:
        rec = _read(path)
        if not rec or rec.get("run") != run:
            return  # not ours anymore (peer reclaimed) — leave it
    try:
        os.unlink(path)
    except OSError:
        pass


def heartbeat(task_id: str) -> None:
    """Refresh the claim mtime so a long run isn't reclaimed under TTL. No-op if
    the file is gone. Never raises."""
    path = _claim_path(task_id)
    try:
        if os.path.exists(path):
            os.utime(path, None)
    except OSError:
        pass


def is_claimed(task_id: str) -> bool:
    """True iff a LIVE claim exists for task_id. Used by the gate to avoid
    waking/pinning a task an executor is actively working."""
    path = _claim_path(task_id)
    if not os.path.exists(path):
        return False
    return _is_live(path)


def list_live() -> list:
    out = []
    try:
        for name in os.listdir(CLAIMS_DIR):
            if not name.endswith(".claim"):
                continue
            tid = name[:-len(".claim")]
            if is_claimed(tid):
                rec = _read(os.path.join(CLAIMS_DIR, name)) or {}
                out.append({"taskId": tid, **{k: rec.get(k) for k in ("pid", "host", "ts", "run")}})
    except OSError:
        pass
    return out


def reap_stale() -> list:
    """Unlink every claim file whose holder is NOT live (dead same-host pid, or
    mtime past TTL). Returns the list of reclaimed task ids. Self-healing janitor:
    keeps the claims dir from accumulating orphan files when a crashed run never
    gets re-attempted. Never raises. Each unlink is guarded by the same flock as
    acquire()'s reclaim path so we don't race a peer mid-acquire."""
    reaped = []
    try:
        names = os.listdir(CLAIMS_DIR)
    except OSError:
        return reaped
    for name in names:
        if not name.endswith(".claim"):
            continue
        path = os.path.join(CLAIMS_DIR, name)
        if _is_live(path):
            continue
        try:
            if fcntl is not None:
                with open(path, "r+", encoding="utf-8") as f:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX)
                    except OSError:
                        continue  # busy -> leave it
                    if _is_live(path):  # re-check under lock (peer may have refreshed)
                        continue
                    os.unlink(path)
            else:
                if not _is_live(path):
                    os.unlink(path)
            reaped.append(name[:-len(".claim")])
        except FileNotFoundError:
            reaped.append(name[:-len(".claim")])  # already gone — fine
        except Exception:
            continue  # never let one bad file abort the sweep
    return reaped


# ── Attempt history / retry cap (ClickUp 86e2ddcpb, 2026-07-24) ────────────
#
# claim_next.py fails open by design: any error in the claim machinery grants
# the claim rather than blocking legitimate work. But that meant there was NO
# upper bound on how many times the SAME task could be reclaimed after
# repeated failures — two tasks looped 12 and ~6 executor sessions
# respectively, each one ending "no commit or push" (never reaching a PR),
# and together tripped the $50/day spend guard, blocking all executor work.
#
# record_attempt()/count_attempts() below give claim_next.py a per-task,
# 24h-rolling attempt count so it can stop reclaiming a task past a cap. This
# is STRICTLY ADDITIVE and preserves the fail-open contract above: every
# function here returns a safe "as if under cap" value on any error — a bug
# in this history bookkeeping must never block a legitimate claim.
CLAIM_HISTORY_DIR = os.path.expanduser("~/.hermes/state/claim_history")
# Cap the number of records kept per task so a pathologically long-lived task
# (already over any sane attempt cap) can't grow its history file unbounded.
MAX_HISTORY_ENTRIES = int(os.environ.get("HERMES_CLAIM_HISTORY_MAX_ENTRIES", "50"))


def _history_path(task_id: str) -> str:
    os.makedirs(CLAIM_HISTORY_DIR, exist_ok=True)
    return os.path.join(CLAIM_HISTORY_DIR, f"{task_id}.json")


def _load_history(task_id: str) -> list:
    """Return the task's attempt history as a list, or [] on ANY error
    (missing file, corrupt JSON, wrong shape, permission error, ...). Never
    raises — callers rely on this to fail open."""
    try:
        path = _history_path(task_id)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            history = json.load(f)
        return history if isinstance(history, list) else []
    except Exception:
        return []


def record_attempt(task_id: str, outcome: str, note: str = "", run: str | None = None) -> None:
    """Append one attempt record for task_id: {"ts", "run", "outcome", "note"}.

    outcome is a short label — "success" | "fail" | "crash" — set by the
    caller (opencode_exec.py) at session end. Counting is by TASK ID, not
    claim run, so a reclaim of a still-fresh task never double-counts (the
    caller passes the same task_id across every reclaim of that task).

    Never raises: any error (disk full, permission, race) is swallowed. A
    history-logging bug must never abort the caller's session-end path."""
    try:
        history = _load_history(task_id)
        history.append({
            "ts": time.time(),
            "run": run or os.environ.get("HERMES_EXECUTOR_RUN_ID", ""),
            "outcome": outcome,
            "note": (note or "")[:500],
        })
        history = history[-MAX_HISTORY_ENTRIES:]
        path = _history_path(task_id)
        tmp = path + f".tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f)
        os.replace(tmp, path)  # atomic rename — no torn reads
    except Exception:
        pass  # FAIL-OPEN: a logging bug must never break the caller


def count_attempts(task_id: str, window_seconds: int) -> int:
    """Count attempts recorded for task_id within the trailing window_seconds.

    FAIL-OPEN: any error (missing/corrupt/unreadable history file) returns 0,
    so a caller comparing this against a cap always resolves to "under cap"
    on failure — the cap logic can never block the queue on its own bug."""
    try:
        history = _load_history(task_id)
        cutoff = time.time() - window_seconds
        count = 0
        for rec in history:
            if not isinstance(rec, dict):
                continue
            try:
                ts = float(rec.get("ts", 0))
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                count += 1
        return count
    except Exception:
        return 0  # FAIL-OPEN


def last_failure_note(task_id: str) -> str:
    """Best-effort: the note from the most recent non-success attempt (for the
    ClickUp over-cap comment). Empty string on any error or if none found."""
    try:
        history = _load_history(task_id)
        for rec in reversed(history):
            if isinstance(rec, dict) and rec.get("outcome") != "success":
                return str(rec.get("note") or "")[:500]
    except Exception:
        pass
    return ""


def _main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "list":
        print(json.dumps(list_live()))
        return 0
    if cmd == "reap":
        print(json.dumps(reap_stale()))
        return 0
    if len(argv) < 2:
        sys.stderr.write(f"usage: claim_store.py {cmd} <taskId>\n")
        return 2
    task_id = argv[1]
    run = None
    if "--run" in argv:
        i = argv.index("--run")
        run = argv[i + 1] if i + 1 < len(argv) else None
    if cmd == "acquire":
        ok = acquire(task_id, run)
        print("acquired" if ok else "live-claimed")
        return 0 if ok else 10
    if cmd == "release":
        release(task_id, run)
        print("released")
        return 0
    if cmd == "heartbeat":
        heartbeat(task_id)
        print("ok")
        return 0
    if cmd == "is-claimed":
        claimed = is_claimed(task_id)
        print("claimed" if claimed else "free")
        return 0 if claimed else 1
    sys.stderr.write(f"unknown command: {cmd}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
