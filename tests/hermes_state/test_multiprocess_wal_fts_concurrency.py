"""Real multi-process SQLite WAL + FTS5 concurrency coverage (86e2abmkq).

Every other concurrency test in this repo (e.g.
``tests/cron/test_scheduler.py``'s ``max_seen`` assertions) drives multiple
*threads* inside a single Python process. That only exercises
``SessionDB._execute_write``'s in-process ``threading.Lock`` — it never
touches the real OS-level file locking SQLite's WAL mode relies on, because
every thread shares one ``sqlite3.Connection`` object.

In production, ``state.db`` is written concurrently by genuinely separate
OS processes sharing one on-disk file: the gateway process, the cron
scheduler/ticker, interactive CLI sessions, and worktree agents (see the
``SessionDB`` docstring in ``hermes_state.py``). That is the concurrency
model this test spawns for real, via ``multiprocessing`` with the ``spawn``
start method (fresh interpreter per worker, matching how these surfaces are
actually launched — never a ``fork`` of a shared parent).

Each worker opens its own on-disk ``SessionDB`` (WAL + FTS5 both live) and
appends messages with a unique FTS-searchable token. After every worker
exits, a fresh reader connection verifies:

  1. Every row landed (``COUNT(*)`` matches expected — no silently dropped
     write survives ``_execute_write``'s BEGIN IMMEDIATE + jitter retry).
  2. Every row is *findable via FTS5 MATCH* (the ``messages_fts`` trigger
     fired for every insert — a corrupt/lagging FTS index under concurrent
     writers would show up here as a "no lost writes but broken search"
     defect distinct from #1).

``test_load_justifies_concurrency_cap_of_four`` is the load test cited in
86e2abmkq's acceptance criteria: it runs the same correctness assertions at
concurrency levels 1, 4, and 8 and records each write's lock-acquisition-to-
commit duration, to give an empirical basis for the ``cron.max_parallel_jobs``
default of 4 (see ``hermes_cli/config.py`` DEFAULT_CONFIG and
``cron/scheduler.py``'s ``tick()``).
"""

from __future__ import annotations

import multiprocessing
import statistics
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


def _mp_append_worker(
    db_path_str: str,
    worker_id: int,
    n_messages: int,
    session_id: str,
    result_queue: "multiprocessing.Queue",
) -> None:
    """Runs in a genuinely separate OS process (spawn context).

    Opens its own on-disk SessionDB connection against the shared db_path,
    appends ``n_messages`` messages each carrying a globally-unique
    FTS-searchable token, and reports timing + errors back via the queue.
    Must be a module-level function so it is picklable under 'spawn'.
    """
    durations: list[float] = []
    errors: list[str] = []
    tokens: list[str] = []
    db = None
    try:
        db = SessionDB(db_path=Path(db_path_str))
        for i in range(n_messages):
            token = f"mpwal{worker_id:03d}x{i:04d}"
            t0 = time.perf_counter()
            try:
                db.append_message(
                    session_id,
                    "user",
                    content=f"payload token={token} from worker {worker_id}",
                )
                tokens.append(token)
            except Exception as exc:  # noqa: BLE001 - report, don't crash worker
                errors.append(f"worker={worker_id} idx={i}: {type(exc).__name__}: {exc}")
            durations.append(time.perf_counter() - t0)
    except Exception as exc:  # noqa: BLE001 - startup failure (open/schema init)
        errors.append(f"worker={worker_id} startup: {type(exc).__name__}: {exc}")
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    result_queue.put(
        {
            "worker_id": worker_id,
            "durations": durations,
            "errors": errors,
            "tokens": tokens,
        }
    )


def _run_concurrent_burst(
    db_path: Path, n_processes: int, n_messages_per_process: int, session_id: str
):
    """Spawn n_processes real OS processes hammering db_path concurrently.

    Returns (results, wall_elapsed_seconds). Each result dict is whatever
    _mp_append_worker put on the queue.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = ctx.Queue()
    procs = []
    t_start = time.perf_counter()
    for worker_id in range(n_processes):
        p = ctx.Process(
            target=_mp_append_worker,
            args=(str(db_path), worker_id, n_messages_per_process, session_id, result_queue),
        )
        procs.append(p)
    for p in procs:
        p.start()

    results = [result_queue.get(timeout=60) for _ in procs]

    for p in procs:
        p.join(timeout=30)
        assert not p.is_alive(), f"worker process {p.pid} did not exit"
        assert p.exitcode == 0, f"worker process exited with {p.exitcode}"
    wall_elapsed = time.perf_counter() - t_start
    return results, wall_elapsed


def _assert_no_lost_writes(db_path: Path, session_id: str, results: list[dict]) -> None:
    """Shared verification: every appended row exists AND is FTS-searchable."""
    all_errors = [e for r in results for e in r["errors"]]
    assert not all_errors, f"worker(s) reported write failures: {all_errors}"

    expected_tokens = {tok for r in results for tok in r["tokens"]}
    assert expected_tokens, "no tokens were recorded — test setup is broken"

    verifier = SessionDB(db_path=db_path)
    try:
        row = verifier._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        actual_count = row[0]
        assert actual_count == len(expected_tokens), (
            f"lost write(s): expected {len(expected_tokens)} messages, "
            f"found {actual_count} in messages table"
        )

        assert verifier._fts_enabled, "FTS5 was not enabled on this SessionDB — test invalid"

        # search_messages() doesn't return a top-level "content" field — the
        # matched text comes back as a highlighted "snippet" (content with
        # '>>>'/'<<<' markers around the match) plus a "context" list of
        # surrounding messages that DO carry "content". Check both.
        missing_from_fts = []
        for token in expected_tokens:
            hits = verifier.search_messages(token, limit=5)
            found = any(
                token in (h.get("snippet") or "")
                or any(token in (c.get("content") or "") for c in h.get("context") or [])
                for h in hits
            )
            if not found:
                missing_from_fts.append(token)
        assert not missing_from_fts, (
            f"{len(missing_from_fts)}/{len(expected_tokens)} messages landed in the "
            f"messages table but are NOT findable via FTS5 MATCH (trigger lag/loss "
            f"under concurrent multi-process writers): {missing_from_fts[:10]}"
        )
    finally:
        verifier.close()


class TestMultiProcessWalFtsNoLostWrites:
    """Correctness gate: matches the resolved cron.max_parallel_jobs cap (4)."""

    def test_four_process_burst_no_lost_writes_and_fts_intact(self, tmp_path):
        db_path = tmp_path / "state.db"
        session_id = "mp-wal-fts-session"

        # Seed the session row up front (search_messages JOINs sessions).
        seeder = SessionDB(db_path=db_path)
        seeder.create_session(session_id, source="cli")
        seeder.close()

        n_processes = 4
        n_messages_per_process = 15
        results, wall_elapsed = _run_concurrent_burst(
            db_path, n_processes, n_messages_per_process, session_id
        )

        assert len(results) == n_processes
        _assert_no_lost_writes(db_path, session_id, results)

        all_durations = [d for r in results for d in r["durations"]]
        assert len(all_durations) == n_processes * n_messages_per_process
        print(
            f"\n[86e2abmkq] 4-process WAL+FTS burst: "
            f"{len(all_durations)} writes in {wall_elapsed:.3f}s wall, "
            f"per-write mean={statistics.mean(all_durations) * 1000:.1f}ms "
            f"p95={sorted(all_durations)[int(len(all_durations) * 0.95)] * 1000:.1f}ms "
            f"max={max(all_durations) * 1000:.1f}ms"
        )


class TestLoadJustifiesConcurrencyCapOfFour:
    """The 86e2abmkq load test: measures real per-write lock-hold/retry
    duration across concurrency levels to justify capping
    ``cron.max_parallel_jobs`` at 4 rather than a higher or unbounded value.

    Correctness (no lost writes) is asserted at every level — the cap isn't
    protecting against corruption (the retry-with-jitter design already
    prevents that up to _WRITE_MAX_RETRIES), it's protecting per-write
    latency: SQLite WAL allows exactly one writer at a time, so beyond a
    small number of concurrent writers, additional processes only queue on
    ``_execute_write``'s BEGIN IMMEDIATE lock and burn their retry budget
    (15 attempts x 20-150ms jitter, i.e. up to ~2.25s worst case) instead of
    adding throughput.
    """

    @pytest.mark.parametrize("n_processes", [1, 4, 8])
    def test_burst_at_concurrency_level(self, tmp_path, n_processes):
        db_path = tmp_path / f"state-{n_processes}.db"
        session_id = f"mp-wal-fts-session-{n_processes}"

        seeder = SessionDB(db_path=db_path)
        seeder.create_session(session_id, source="cli")
        seeder.close()

        n_messages_per_process = 10
        results, wall_elapsed = _run_concurrent_burst(
            db_path, n_processes, n_messages_per_process, session_id
        )

        # Correctness holds at every concurrency level tested — the cap is a
        # latency/efficiency choice, not a correctness requirement below 8.
        _assert_no_lost_writes(db_path, session_id, results)

        all_durations = [d for r in results for d in r["durations"]]
        total_writes = n_processes * n_messages_per_process
        mean_ms = statistics.mean(all_durations) * 1000
        max_ms = max(all_durations) * 1000
        throughput = total_writes / wall_elapsed
        print(
            f"\n[86e2abmkq load test] n_processes={n_processes} "
            f"writes={total_writes} wall={wall_elapsed:.3f}s "
            f"throughput={throughput:.1f} writes/s "
            f"per-write mean={mean_ms:.1f}ms max={max_ms:.1f}ms"
        )

        # Sanity bound: even the most contended write must stay well inside
        # the retry budget (up to ~2.25s worst case: 15 retries x <=150ms),
        # never approaching retry exhaustion. This is the evidence that a
        # burst at the chosen cap (4) — and even double that (8) — never
        # gets close to the failure boundary the retry budget exists to
        # avoid, i.e. 4 is comfortably conservative, not a hidden cliff.
        assert max_ms < 2250, (
            f"n_processes={n_processes}: slowest write took {max_ms:.1f}ms, "
            f"approaching the ~2.25s retry-exhaustion ceiling"
        )
