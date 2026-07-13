"""Tests for cronjob no_agent mode — script-driven jobs that skip the LLM.

Covers:

* ``create_job(no_agent=True)`` shape, validation, and serialization.
* ``cronjob(action='create', no_agent=True)`` tool-level validation.
* ``cronjob(action='update')`` flipping no_agent on/off.
* ``scheduler.run_job`` short-circuit path: success/silent/failure.
* Shell script support in ``_run_job_script`` (.sh runs via bash).
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


# ---------------------------------------------------------------------------
# create_job / update_job: data-layer semantics
# ---------------------------------------------------------------------------


def test_create_job_no_agent_requires_script(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="no_agent=True requires a script"):
        create_job(prompt=None, schedule="every 5m", no_agent=True)


def test_create_job_no_agent_stores_field(hermes_env):
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "watchdog.sh"
    script_path.write_text("#!/bin/bash\necho hi\n")

    job = create_job(
        prompt=None,
        schedule="every 5m",
        script="watchdog.sh",
        no_agent=True,
        deliver="local",
    )
    assert job["no_agent"] is True
    assert job["script"] == "watchdog.sh"
    # Prompt can be empty/None for no_agent jobs.
    assert job["prompt"] in {None, ""}


def test_create_job_default_is_not_no_agent(hermes_env):
    from cron.jobs import create_job

    job = create_job(prompt="say hi", schedule="every 5m", deliver="local")
    assert job.get("no_agent") is False


def test_update_job_roundtrips_no_agent_flag(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")
    job = create_job(prompt=None, schedule="every 5m", script="w.sh", no_agent=True, deliver="local")

    update_job(job["id"], {"no_agent": False})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is False

    update_job(job["id"], {"no_agent": True})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is True


# ---------------------------------------------------------------------------
# cronjob tool: API-layer validation
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_no_agent_without_script_errors(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(action="create", schedule="every 5m", no_agent=True, deliver="local")
    )
    assert result.get("success") is False
    assert "no_agent=True requires a script" in result.get("error", "")


def test_cronjob_tool_create_no_agent_with_script_succeeds(hermes_env):
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho alert\n")

    result = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="alert.sh",
            no_agent=True,
            deliver="local",
        )
    )
    assert result.get("success") is True
    assert result["job"]["no_agent"] is True
    assert result["job"]["script"] == "alert.sh"


def test_cronjob_tool_update_toggles_no_agent(hermes_env):
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")

    created = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="w.sh",
            no_agent=True,
            deliver="local",
        )
    )
    job_id = created["job_id"]

    off = json.loads(cronjob(action="update", job_id=job_id, no_agent=False, prompt="run"))
    assert off["success"] is True
    assert off["job"].get("no_agent") in {False, None}

    on = json.loads(cronjob(action="update", job_id=job_id, no_agent=True))
    assert on["success"] is True
    assert on["job"]["no_agent"] is True


def test_cronjob_tool_update_no_agent_without_script_errors(hermes_env):
    """Flipping no_agent=True on a job that has no script must fail."""
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(action="create", schedule="every 5m", prompt="do a thing", deliver="local")
    )
    job_id = created["job_id"]

    result = json.loads(cronjob(action="update", job_id=job_id, no_agent=True))
    assert result.get("success") is False
    assert "without a script" in result.get("error", "")


def test_cronjob_tool_create_does_not_require_prompt_when_no_agent(hermes_env):
    """The 'prompt or skill required' rule is relaxed for no_agent jobs."""
    from tools.cronjob_tools import cronjob

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")

    result = json.loads(
        cronjob(
            action="create",
            schedule="every 5m",
            script="w.sh",
            no_agent=True,
            deliver="local",
        )
    )
    assert result.get("success") is True


# ---------------------------------------------------------------------------
# scheduler.run_job: short-circuit behavior
# ---------------------------------------------------------------------------


def test_run_job_no_agent_success_returns_script_stdout(hermes_env):
    """Happy path: script exits 0 with output, delivered verbatim."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'RAM 92% on host'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert "RAM 92% on host" in final_response
    assert "RAM 92% on host" in doc


def test_run_job_no_agent_empty_output_is_silent(hermes_env):
    """Empty stdout → SILENT_MARKER, which suppresses delivery downstream."""
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    script_path = hermes_env / "scripts" / "quiet.sh"
    script_path.write_text("#!/bin/bash\n# nothing to say\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="quiet.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert final_response == SILENT_MARKER


def test_run_job_no_agent_wake_gate_is_silent(hermes_env):
    """wakeAgent=false gate in stdout triggers a silent run."""
    from cron.jobs import create_job
    from cron.scheduler import run_job, SILENT_MARKER

    script_path = hermes_env / "scripts" / "gated.sh"
    script_path.write_text('#!/bin/bash\necho \'{"wakeAgent": false}\'\n')

    job = create_job(
        prompt=None, schedule="every 5m", script="gated.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert final_response == SILENT_MARKER


def test_run_job_no_agent_script_failure_delivers_error(hermes_env):
    """Non-zero exit → success=False, error alert is the delivered message."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "broken.sh"
    script_path.write_text("#!/bin/bash\necho oops >&2\nexit 3\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="broken.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is False
    assert error is not None
    assert "oops" in final_response or "exited with code 3" in final_response
    assert "Cron watchdog" in final_response  # alert header


def test_run_job_no_agent_never_invokes_aiagent(hermes_env):
    """no_agent jobs must NOT import/construct the AIAgent."""
    from cron.jobs import create_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho alert\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )

    with patch("run_agent.AIAgent") as ai_mock:
        from cron.scheduler import run_job

        run_job(job)

    ai_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _run_job_script: shell-script support
# ---------------------------------------------------------------------------


def test_run_job_script_shell_script_runs_via_bash(hermes_env):
    """.sh files should execute under /bin/bash even without a shebang line."""
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "shelly.sh"
    # No shebang — relies on the interpreter-by-extension rule.
    script_path.write_text('echo "shell: $BASH_VERSION" | head -c 7\n')

    ok, output = _run_job_script("shelly.sh")
    assert ok is True
    assert output.startswith("shell:")


def test_run_job_script_bash_extension_also_runs_via_bash(hermes_env):
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "thing.bash"
    script_path.write_text('printf "via bash\\n"\n')

    ok, output = _run_job_script("thing.bash")
    assert ok is True
    assert output == "via bash"


def test_run_job_script_python_still_runs_via_python(hermes_env):
    """Regression: .py files must keep running via sys.executable."""
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "py.py"
    script_path.write_text("import sys\nprint(f'python {sys.version_info.major}')\n")

    ok, output = _run_job_script("py.py")
    assert ok is True
    assert output.startswith("python ")


def test_run_job_script_path_traversal_still_blocked(hermes_env):
    """Security regression: shell-script support must NOT loosen containment."""
    from cron.scheduler import _run_job_script

    # Absolute path outside the scripts dir should be rejected.
    ok, output = _run_job_script("/etc/passwd")
    assert ok is False
    assert "Blocked" in output or "outside" in output


# ---------------------------------------------------------------------------
# _run_job_script: flag-gated, per-job lazy secret injection
# ---------------------------------------------------------------------------


def _write_secret_probe(hermes_env, filename, variable):
    script_path = hermes_env / "scripts" / filename
    script_path.write_text(
        "import os\n"
        f"print('PRESENT' if os.environ.get({variable!r}) else 'ABSENT')\n"
    )
    return script_path


def test_run_job_script_lazy_injects_declared_secret_into_child_only(
    hermes_env, monkeypatch
):
    from cron.scheduler import _run_job_script

    _write_secret_probe(hermes_env, "clickup_probe.py", "CLICKUP_API_TOKEN")
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "1")
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

    with patch("agent.lazy_secret_resolver.get", return_value="pk_test_clickup") as lazy_get:
        ok, output = _run_job_script(
            "clickup_probe.py",
            required_environment_variables=["CLICKUP_API_TOKEN"],
        )

    assert ok is True
    assert output == "PRESENT"
    assert "CLICKUP_API_TOKEN" not in os.environ
    lazy_get.assert_called_once_with("CLICKUP_API_TOKEN")


def test_run_job_script_injects_declared_secret_from_profile_scope(
    hermes_env, monkeypatch
):
    from agent import secret_scope as ss
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "profile_scope_probe.py"
    script_path.write_text(
        "import os\n"
        "value = os.environ.get('CLICKUP_API_TOKEN')\n"
        "print('PROFILE' if value == 'profile-scoped-clickup-token' "
        "else 'BOOT' if value == 'boot-clickup-token' else 'ABSENT')\n"
    )
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "1")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "boot-clickup-token")

    prior_multiplex = ss.is_multiplex_active()
    ss.set_multiplex_active(True)
    scope_token = ss.set_secret_scope(
        {"CLICKUP_API_TOKEN": "profile-scoped-clickup-token"}
    )
    try:
        with patch(
            "agent.lazy_secret_resolver.get", return_value="wrong-global-token"
        ) as lazy_get:
            ok, output = _run_job_script(
                "profile_scope_probe.py",
                required_environment_variables=["CLICKUP_API_TOKEN"],
            )
    finally:
        ss.reset_secret_scope(scope_token)
        ss.set_multiplex_active(prior_multiplex)

    assert ok is True
    assert output == "PROFILE"
    assert os.environ["CLICKUP_API_TOKEN"] == "boot-clickup-token"
    lazy_get.assert_not_called()


def test_run_job_script_multiplex_scope_miss_never_uses_global_lazy_resolver(
    hermes_env, monkeypatch
):
    from agent import secret_scope as ss
    from cron.scheduler import _run_job_script

    _write_secret_probe(hermes_env, "multiplex_miss_probe.py", "CLICKUP_API_TOKEN")
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "1")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "default-profile-boot-token")

    prior_multiplex = ss.is_multiplex_active()
    ss.set_multiplex_active(True)
    scope_token = ss.set_secret_scope({})
    try:
        with patch(
            "agent.lazy_secret_resolver.get", return_value="must-not-cross-profiles"
        ) as lazy_get:
            ok, output = _run_job_script(
                "multiplex_miss_probe.py",
                required_environment_variables=["CLICKUP_API_TOKEN"],
            )
    finally:
        ss.reset_secret_scope(scope_token)
        ss.set_multiplex_active(prior_multiplex)

    assert ok is True
    assert output == "ABSENT"
    lazy_get.assert_not_called()


def test_run_job_script_unscoped_ambient_secret_keeps_precedence(
    hermes_env, monkeypatch
):
    from agent import secret_scope as ss
    from cron.scheduler import _run_job_script

    _write_secret_probe(hermes_env, "ambient_precedence.py", "CLICKUP_API_TOKEN")
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "1")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "ambient-clickup-token")

    prior_multiplex = ss.is_multiplex_active()
    ss.set_multiplex_active(False)
    scope_token = ss.set_secret_scope(None)
    try:
        with patch(
            "agent.lazy_secret_resolver.get", return_value="unused-lazy-token"
        ) as lazy_get:
            ok, output = _run_job_script(
                "ambient_precedence.py",
                required_environment_variables=["CLICKUP_API_TOKEN"],
            )
    finally:
        ss.reset_secret_scope(scope_token)
        ss.set_multiplex_active(prior_multiplex)

    assert ok is True
    assert output == "PRESENT"
    lazy_get.assert_not_called()


def test_run_job_script_lazy_resolution_is_inert_when_flag_off(
    hermes_env, monkeypatch
):
    from cron.scheduler import _run_job_script

    _write_secret_probe(hermes_env, "flag_off_probe.py", "CLICKUP_API_TOKEN")
    monkeypatch.delenv("HERMES_LAZY_SECRET_RESOLUTION", raising=False)
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

    with patch("agent.lazy_secret_resolver.get", return_value="must-not-resolve") as lazy_get:
        ok, output = _run_job_script(
            "flag_off_probe.py",
            required_environment_variables=["CLICKUP_API_TOKEN"],
        )

    assert ok is True
    assert output == "ABSENT"
    lazy_get.assert_not_called()


def test_run_job_script_lazy_resolution_failure_is_fail_open(
    hermes_env, monkeypatch
):
    from cron.scheduler import _run_job_script

    _write_secret_probe(hermes_env, "resolver_failure.py", "CLICKUP_API_TOKEN")
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "true")
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

    with patch(
        "agent.lazy_secret_resolver.get",
        side_effect=RuntimeError("resolver unavailable"),
    ):
        ok, output = _run_job_script(
            "resolver_failure.py",
            required_environment_variables=["CLICKUP_API_TOKEN"],
        )

    assert ok is True
    assert output == "ABSENT"


def test_run_job_script_redacts_child_only_lazy_secret(hermes_env, monkeypatch):
    from cron.scheduler import _run_job_script

    opaque_secret = "opaque-value-with-no-token-shape"
    script_path = hermes_env / "scripts" / "print_secret.py"
    script_path.write_text("import os\nprint(os.environ['CLICKUP_API_TOKEN'])\n")
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "1")
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

    with patch("agent.lazy_secret_resolver.get", return_value=opaque_secret):
        ok, output = _run_job_script(
            "print_secret.py",
            required_environment_variables=["CLICKUP_API_TOKEN"],
        )

    assert ok is True
    assert opaque_secret not in output
    assert "[REDACTED]" in output


def test_run_job_script_accepts_skill_style_secret_declarations(
    hermes_env, monkeypatch
):
    from cron.scheduler import _run_job_script

    _write_secret_probe(hermes_env, "mapping_declaration.py", "CLICKUP_API_TOKEN")
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "yes")
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

    declarations = [
        {"name": "CLICKUP_API_TOKEN", "help": "ClickUp personal token"},
        {"name": "not a valid env name"},
    ]
    with patch("agent.lazy_secret_resolver.get", return_value="pk_test_clickup") as lazy_get:
        ok, output = _run_job_script(
            "mapping_declaration.py",
            required_environment_variables=declarations,
        )

    assert ok is True
    assert output == "PRESENT"
    lazy_get.assert_called_once_with("CLICKUP_API_TOKEN")


def test_run_job_script_declaration_does_not_bypass_provider_blocklist(
    hermes_env, monkeypatch
):
    from cron.scheduler import _run_job_script

    _write_secret_probe(hermes_env, "provider_probe.py", "ANTHROPIC_API_KEY")
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch("agent.lazy_secret_resolver.get", return_value="provider-secret") as lazy_get:
        ok, output = _run_job_script(
            "provider_probe.py",
            required_environment_variables=["ANTHROPIC_API_KEY"],
        )

    assert ok is True
    assert output == "ABSENT"
    lazy_get.assert_not_called()


@pytest.mark.parametrize(
    ("job_name", "script_name"),
    [
        ("clickup-workspace-refresh", "clickup_workspace_refresh.py"),
        ("clickup-review-sla", "clickup_review_sla.py"),
        ("staleness-sweep", "staleness_sweep.py"),
    ],
)
def test_no_agent_clickup_health_jobs_forward_declared_secret(
    hermes_env, job_name, script_name
):
    from cron.scheduler import run_job

    job = {
        "id": f"test-{job_name}",
        "name": job_name,
        "prompt": "",
        "script": script_name,
        "no_agent": True,
        "required_environment_variables": ["CLICKUP_API_TOKEN"],
    }

    with patch("cron.scheduler._run_job_script", return_value=(True, "ok")) as runner:
        success, _doc, final_response, error = run_job(job)

    assert success is True
    assert final_response == "ok"
    assert error is None
    runner.assert_called_once_with(
        script_name,
        required_environment_variables=["CLICKUP_API_TOKEN"],
    )
