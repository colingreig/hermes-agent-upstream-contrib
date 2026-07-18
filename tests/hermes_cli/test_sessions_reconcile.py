"""Tests for the ``hermes sessions reconcile`` subcommand (86e2abmm6).

Mirrors the style of tests/hermes_cli/test_sessions_delete.py: a FakeDB
stand-in for SessionDB, driven end-to-end through hermes_cli.main.main().
"""

import sys


def test_sessions_reconcile_dry_run_reports_classification_and_never_applies(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    import hermes_state

    captured = {}

    class FakeDB:
        def classify_stale_sessions(self, **kwargs):
            captured["classify_kwargs"] = kwargs
            return [
                {
                    "id": "cron_stale-job_20260101_000000",
                    "source": "cron",
                    "started_at": 0.0,
                    "age_seconds": 7200.0,
                    "reason": "cron-stale-active",
                    "candidate": True,
                },
                {
                    "id": "cron_fresh-job_20260101_000000",
                    "source": "cron",
                    "started_at": 0.0,
                    "age_seconds": 10.0,
                    "reason": "cron-recent-active",
                    "candidate": False,
                },
            ]

        def reconcile_stale_sessions(self, **kwargs):
            raise AssertionError("reconcile_stale_sessions must not run without --apply")

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: FakeDB())
    monkeypatch.setattr(
        sys, "argv",
        ["hermes", "sessions", "reconcile", "--min-age-seconds", "3600"],
    )

    main_mod.main()

    output = capsys.readouterr().out
    assert captured["classify_kwargs"] == {"source": "cron", "min_age_seconds": 3600}
    assert captured["closed"] is True
    assert "Dry run" in output
    assert "cron-stale-active: 1 [candidate for --apply]" in output
    assert "cron-recent-active: 1" in output
    assert "cron_stale-job_20260101_000000" in output
    assert "--apply" in output


def test_sessions_reconcile_dry_run_reports_no_candidates(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    import hermes_state

    class FakeDB:
        def classify_stale_sessions(self, **kwargs):
            return []

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: FakeDB())
    monkeypatch.setattr(sys, "argv", ["hermes", "sessions", "reconcile"])

    main_mod.main()

    output = capsys.readouterr().out
    assert "No candidates" in output


def test_sessions_reconcile_apply_calls_backup_first_reconcile_and_reports_counts(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    import hermes_state

    captured = {}

    class FakeDB:
        def reconcile_stale_sessions(self, **kwargs):
            captured["apply_kwargs"] = kwargs
            return {
                "backup_path": "/tmp/state.db.reconcile-backup-20260101_000000",
                "classified": 2,
                "candidates": 1,
                "before_active": 3,
                "after_active": 2,
                "closed": 1,
            }

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: FakeDB())
    monkeypatch.setattr(
        sys, "argv",
        ["hermes", "sessions", "reconcile", "--apply", "--min-age-seconds", "60"],
    )

    main_mod.main()

    output = capsys.readouterr().out
    assert captured["apply_kwargs"] == {
        "source": "cron",
        "min_age_seconds": 60,
        "end_reason": "cron_reconciled",
        "backup": True,
    }
    assert "Backup: /tmp/state.db.reconcile-backup-20260101_000000" in output
    assert "before: 3" in output
    assert "after: 2" in output
    assert "closed: 1" in output


def test_sessions_reconcile_apply_no_backup_flag_disables_backup(monkeypatch, capsys):
    import hermes_cli.main as main_mod
    import hermes_state

    captured = {}

    class FakeDB:
        def reconcile_stale_sessions(self, **kwargs):
            captured["apply_kwargs"] = kwargs
            return {
                "backup_path": None,
                "classified": 0,
                "candidates": 0,
                "before_active": 0,
                "after_active": 0,
                "closed": 0,
            }

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", lambda: FakeDB())
    monkeypatch.setattr(
        sys, "argv",
        ["hermes", "sessions", "reconcile", "--apply", "--no-backup"],
    )

    main_mod.main()

    assert captured["apply_kwargs"]["backup"] is False
