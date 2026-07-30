#!/usr/bin/env python3
"""
mcp_serve_reaper.py — standalone liveness sweep for orphaned `hermes mcp serve` procs.

WHY THIS EXISTS (task 86e2hap4g): every per-session `hermes mcp serve` stdio subprocess
(spawned by an MCP client — Claude Code / Codex over SSH — one per session) lingers when
the client disconnects ungracefully (dropped SSH, laptop sleep, killed task). Nothing on
the mini reaped these; 5 orphans from 2 retired releases were found and killed by hand in
one pass. The release path is baked into each process at spawn time, so a `runtime-current`
symlink flip never touches them either way.

ADVERSARIALLY REJECTED APPROACH — do not "fix" this by killing on release-path mismatch.
On this actively-shipping fleet, "release path != current" mostly selects processes started
before the LAST deploy, i.e. healthy in-progress Claude Code / Codex sessions. A release-cut-
tied reaper would TERM/KILL live work the moment a deploy lands. The release poller is also
unreliable (see the mini-release-cut/poll fixes shipped alongside this task), so a cut-tied
reaper might not even run reliably.

THE ACTUAL SIGNAL THIS SCRIPT USES: liveness, not release identity.
  - **Dead parent.** A process orphaned by a dropped SSH session is reparented to PID 1
    by the kernel. `ppid == 1` is therefore the primary, reliable orphan signal on macOS.
  - **Dead owning sshd session.** Some intermediate wrapper can sit between the mcp-serve
    process and its `sshd-session:` ancestor. This script walks the parent chain looking
    for a live `sshd-session:` process; if the walk reaches PID 1 without finding one, the
    owning session is gone even though the immediate parent isn't literally PID 1 yet.
  - **Age floor.** Both signals race a legitimate reconnect (a client that drops and
    re-attaches within seconds). A process younger than `--min-age-minutes` (default 45)
    is never touched, regardless of its liveness signal.

Only processes matching BOTH "genuinely orphaned" AND "old enough" are reaped, and only via
TERM-then-grace-then-KILL (never a bare SIGKILL) so a process gets a chance to flush and exit
cleanly. This mirrors the redundant, conservative, fail-closed posture of
`worktree_backstop_sweep.py`'s age-based safety net — same philosophy, different resource.

Usage:
  python3 mcp_serve_reaper.py [--dry-run] [--min-age-minutes 45] [--grace-seconds 10]

Exit code is 0 only when the process snapshot was readable and every selected
reap completed.  Snapshot and per-process failures are non-zero so the
independent fleet outcome probe can distinguish "nothing to reap" from "the
reaper could not inspect or enforce its policy."
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import time

PROC_PATTERN = re.compile(r"\bhermes\s+mcp\s+serve\b")
SSHD_SESSION_PATTERN = re.compile(r"^sshd-session:")
MAX_ANCESTOR_HOPS = 25


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"[{ts}] {msg}", flush=True)


def _ps_snapshot():
    """Return {pid: {"ppid": int, "etime_seconds": int, "command": str}} for every
    process, via one `ps` call (cheaper and race-free vs. per-pid lookups).

    `etime=` (not GNU's `etimes=`) is deliberate: this script targets the mini's
    BSD `ps`, which has no `etimes` keyword and exits nonzero rather than
    silently substituting it — `etimes` would make every snapshot fail closed
    to empty."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except Exception as exc:
        _log(f"ERROR_PS_SNAPSHOT: {exc}")
        return None

    snapshot = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, etime_s, command = parts
        try:
            pid, ppid = int(pid_s), int(ppid_s)
            etime_seconds = _parse_bsd_etime(etime_s)
        except ValueError:
            continue
        snapshot[pid] = {"ppid": ppid, "etime_seconds": etime_seconds, "command": command}
    return snapshot


def _parse_bsd_etime(etime: str) -> int:
    """Parse BSD `ps -o etime=` output into whole seconds. Formats observed on
    macOS: `ss`, `mm:ss`, `hh:mm:ss`, `dd-hh:mm:ss`."""
    days = 0
    rest = etime
    if "-" in etime:
        days_s, rest = etime.split("-", 1)
        days = int(days_s)
    fields = [int(f) for f in rest.split(":")]
    while len(fields) < 3:
        fields.insert(0, 0)
    hours, minutes, seconds = fields[-3], fields[-2], fields[-1]
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _find_mcp_serve_candidates(snapshot):
    return [
        pid for pid, info in snapshot.items()
        if PROC_PATTERN.search(info["command"])
    ]


def _is_orphaned(pid, snapshot):
    """True if the process's parent chain never reaches a live `sshd-session:` process
    before hitting PID 1. Bails (returns False — never reap) on any ambiguity: a missing
    ancestor entry, or a chain longer than MAX_ANCESTOR_HOPS, is treated as "not proven
    orphaned" rather than "assume orphaned"."""
    info = snapshot.get(pid)
    if info is None:
        return False
    if info["ppid"] == 1:
        return True

    current = info["ppid"]
    for _ in range(MAX_ANCESTOR_HOPS):
        if current == 1:
            return True  # walked all the way to init without finding a live session
        ancestor = snapshot.get(current)
        if ancestor is None:
            return False  # can't prove it — protect the candidate
        if SSHD_SESSION_PATTERN.match(ancestor["command"]):
            return False  # owning session is alive
        current = ancestor["ppid"]
    return False  # chain too deep / cyclic — protect the candidate


def _reap(pid, command, grace_seconds, dry_run):
    if dry_run:
        _log(f"WOULD_REAP: pid={pid} command={command!r}")
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _log(f"ALREADY_GONE: pid={pid}")
        return True
    except Exception as exc:
        _log(f"ERROR_SIGTERM: pid={pid} | {exc}")
        return False

    _log(f"SIGTERM_SENT: pid={pid} | grace={grace_seconds}s")
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        time.sleep(1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _log(f"REAPED_CLEAN: pid={pid}")
            return True
        except Exception:
            break

    try:
        os.kill(pid, signal.SIGKILL)
        _log(f"SIGKILL_SENT: pid={pid} (did not exit within grace period)")
    except ProcessLookupError:
        _log(f"REAPED_CLEAN: pid={pid} (exited just before SIGKILL)")
    except Exception as exc:
        _log(f"ERROR_SIGKILL: pid={pid} | {exc}")
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="log candidates without signaling anything")
    parser.add_argument("--min-age-minutes", type=int, default=45,
                         help="age floor to avoid flip-timing races (default: 45)")
    parser.add_argument("--grace-seconds", type=int, default=10,
                         help="seconds to wait after SIGTERM before SIGKILL (default: 10)")
    args = parser.parse_args(argv)

    _log(
        f"sweep-start min_age_minutes={args.min_age_minutes} "
        f"grace_seconds={args.grace_seconds} dry_run={args.dry_run}"
    )
    snapshot = _ps_snapshot()
    if snapshot is None:
        return 2
    if not snapshot:
        _log("sweep-finish reaped=0 skipped_young=0 skipped_live=0 (empty ps snapshot)")
        return 0

    candidates = _find_mcp_serve_candidates(snapshot)
    reaped = 0
    skipped_young = 0
    skipped_live = 0
    failed = 0
    min_age_seconds = args.min_age_minutes * 60

    for pid in sorted(candidates):
        info = snapshot[pid]
        age_seconds = info["etime_seconds"]
        if age_seconds < min_age_seconds:
            skipped_young += 1
            _log(f"SKIP_YOUNG: pid={pid} age_minutes={age_seconds // 60}")
            continue
        if not _is_orphaned(pid, snapshot):
            skipped_live += 1
            _log(f"SKIP_LIVE: pid={pid} (owning session still alive)")
            continue
        if _reap(pid, info["command"], args.grace_seconds, args.dry_run):
            reaped += 1
        else:
            failed += 1

    _log(
        f"sweep-finish candidates={len(candidates)} reaped={reaped} "
        f"skipped_young={skipped_young} skipped_live={skipped_live} "
        f"failed={failed} dry_run={args.dry_run}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
