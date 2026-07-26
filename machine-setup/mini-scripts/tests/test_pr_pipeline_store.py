"""Transactional, fencing, fault, and concurrency proof for TrustStore."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_pipeline.identity import TrustedMergeIdentity  # noqa: E402
from pr_pipeline.store import (  # noqa: E402
    FinalizationConflictError, LeaseBusyError, LeaseLostError, TrustStore,
)


def identity(**overrides: object) -> TrustedMergeIdentity:
    values: dict[str, object] = {
        "canonical_repo": "Acme/Widget", "pr_number": 7, "trusted_task_id": "86e2gh04e",
        "base_sha": "a" * 40, "head_sha": "b" * 40, "tested_merge_sha": "c" * 40,
        "ci_policy_id": "sha256:" + "d" * 64, "ci_run_ids": ("102", "101"),
    }
    values.update(overrides)
    return TrustedMergeIdentity(**values)  # type: ignore[arg-type]


class TrustStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.sqlite3"

    def test_wal_identity_and_monotonic_fencing_are_real_persisted_invariants(self) -> None:
        store = TrustStore(self.path)
        first = store.acquire(identity(), holder="review-a", ttl_s=10, now_ms=1_000)
        self.assertEqual(first.identity.canonical_repo, "acme/widget")
        self.assertEqual(first.identity.ci_run_ids, ("101", "102"))
        second = store.acquire(identity(head_sha="e" * 40, tested_merge_sha="f" * 40), holder="review-b", ttl_s=10, now_ms=11_001)
        self.assertGreater(second.fencing_token, first.fencing_token)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(connection.execute("SELECT next_fencing_token FROM subjects").fetchone()[0], second.fencing_token)

    def test_expired_or_superseded_worker_cannot_finalize_and_terminal_block_is_immutable(self) -> None:
        store = TrustStore(self.path)
        first = store.acquire(identity(), holder="review-a", ttl_s=1, now_ms=1_000)
        second = store.acquire(identity(), holder="review-b", ttl_s=10, now_ms=2_001)
        with self.assertRaises(LeaseLostError):
            store.finalize(first, "passed", {"attempt": 1}, now_ms=2_002)
        written = store.finalize(second, "BLOCK", {"reason": "test failure"}, now_ms=2_003)
        self.assertTrue(written.accepted)
        self.assertEqual(written.verdict, "BLOCK")
        with self.assertRaises(FinalizationConflictError):
            store.finalize(second, "passed", {"attempt": 2}, now_ms=2_004)
        self.assertEqual(store.get_finalization(identity()).evidence["reason"], "test failure")  # type: ignore[union-attr]

    def test_concurrent_acquirers_have_one_winner_not_two_unfenced_reviewers(self) -> None:
        first, second = TrustStore(self.path, busy_timeout_ms=2_000), TrustStore(self.path, busy_timeout_ms=2_000)
        gate = threading.Barrier(2)
        outcomes: list[object] = []

        def acquire(store: TrustStore, holder: str) -> None:
            gate.wait()
            try:
                outcomes.append(store.acquire(identity(), holder=holder, ttl_s=10, now_ms=1_000))
            except LeaseBusyError:
                outcomes.append("busy")

        threads = [threading.Thread(target=acquire, args=(first, "one")), threading.Thread(target=acquire, args=(second, "two"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(not isinstance(result, str) for result in outcomes), 1)
        self.assertEqual(outcomes.count("busy"), 1)

    def test_actual_sqlite_abort_rolls_back_identity_and_subject_without_half_lease(self) -> None:
        store = TrustStore(self.path)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_lease_insert BEFORE INSERT ON leases "
                "BEGIN SELECT RAISE(ABORT, 'simulated storage fault'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            store.acquire(identity(), holder="review-a", ttl_s=10, now_ms=1_000)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM identities").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM subjects").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM leases").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
