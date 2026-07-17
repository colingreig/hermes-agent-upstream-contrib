"""Tests for the per-job ``no_fallback`` fail-closed pin (86e2bjac3) at the
job-schema layer.

Covers:

* ``_coerce_job_bool`` — the shared coercer that normalizes hand-edited/JSON/
  CLI boolean spellings for the field.
* ``create_job(no_fallback=...)`` persistence, including the default.
* ``update_job(..., {"no_fallback": ...})`` roundtrip + string coercion.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs don't leak into real state."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)

    return home


# ---------------------------------------------------------------------------
# _coerce_job_bool
# ---------------------------------------------------------------------------


class TestCoerceJobBool:
    def test_bool_true_passthrough(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool(True) is True

    def test_bool_false_passthrough(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool(False) is False

    def test_string_true(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool("true") is True

    def test_string_false(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool("false") is False

    def test_string_1(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool("1") is True

    def test_string_0(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool("0") is False

    def test_none_uses_default_false(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool(None) is False

    def test_none_uses_default_true(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool(None, default=True) is True

    def test_string_yes(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool("yes") is True

    def test_int_1(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool(1) is True

    def test_int_0(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool(0) is False

    def test_uppercase_string(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool("TRUE") is True
        assert _coerce_job_bool("False") is False

    def test_unrecognized_string_uses_default(self):
        from cron.jobs import _coerce_job_bool
        assert _coerce_job_bool("maybe", default=False) is False
        assert _coerce_job_bool("maybe", default=True) is True


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


def test_create_job_no_fallback_true_persists(hermes_env):
    from cron.jobs import create_job

    job = create_job(prompt="say hi", schedule="every 5m", no_fallback=True, deliver="local")
    assert job["no_fallback"] is True


def test_create_job_default_no_fallback_is_false(hermes_env):
    from cron.jobs import create_job

    job = create_job(prompt="say hi", schedule="every 5m", deliver="local")
    assert job["no_fallback"] is False


# ---------------------------------------------------------------------------
# update_job
# ---------------------------------------------------------------------------


def test_update_job_no_fallback_string_true_coerces(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    job = create_job(prompt="say hi", schedule="every 5m", deliver="local")

    update_job(job["id"], {"no_fallback": "true"})
    reloaded = get_job(job["id"])
    assert reloaded["no_fallback"] is True


def test_update_job_no_fallback_string_false_coerces(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    job = create_job(prompt="say hi", schedule="every 5m", no_fallback=True, deliver="local")

    update_job(job["id"], {"no_fallback": "false"})
    reloaded = get_job(job["id"])
    assert reloaded["no_fallback"] is False


def test_update_job_no_fallback_roundtrips_bool(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    job = create_job(prompt="say hi", schedule="every 5m", deliver="local")

    update_job(job["id"], {"no_fallback": True})
    assert get_job(job["id"])["no_fallback"] is True

    update_job(job["id"], {"no_fallback": False})
    assert get_job(job["id"])["no_fallback"] is False
