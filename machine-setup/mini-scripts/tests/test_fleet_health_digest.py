"""Executable security contract for the deterministic fleet health digest."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]
DIGEST = SCRIPTS / "fleet_health_digest.py"


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_digest_composes_fresh_body_and_delivers_only_to_fixed_recipient(monkeypatch, tmp_path, capsys):
    module = _load("fleet_health_digest_test", DIGEST)
    captured = {}
    monkeypatch.setattr(module, "run_folded_checks", lambda: [])

    monkeypatch.setattr(
        module.builder,
        "compose_report",
        lambda **kwargs: {
            "subject": "Hermes fresh status",
            "text_body": f"fresh slot={kwargs['scheduled_slot']}",
            "html_body": "<p>fresh</p>",
            "summary": {"work_list_n": 3},
        },
    )
    monkeypatch.setattr(module.sender, "_resolve_token", lambda: "test-token")

    def fake_send(token, args, text_body, html_body):
        captured.update(token=token, args=args, text=text_body, html=html_body)
        return True, "mock-message-id", None

    monkeypatch.setattr(module.sender, "_send_postmark", fake_send)
    monkeypatch.setattr(module.sender, "_write_receipt", lambda *args: (_ for _ in ()).throw(AssertionError("digest must not write receipts")))
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert module.main(["--scheduled-slot", "2026-08-03T12:00:00Z"]) == 0

    assert captured["args"].to == "colin@colingreig.com"
    assert captured["args"].fallback_to == "slack:D0BA2PM9CFM"
    assert captured["text"] == "fresh slot=2026-08-03T12:00:00Z"
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    result = json.loads(capsys.readouterr().out)
    assert result == {"channel": "postmark", "message_id": "mock-message-id", "status": "sent"}


def test_digest_uses_fixed_fallback_and_never_accepts_recipient_or_body_path(monkeypatch):
    module = _load("fleet_health_digest_fallback_test", DIGEST)
    monkeypatch.setattr(module, "run_folded_checks", lambda: [])
    monkeypatch.setattr(module.builder, "compose_report", lambda **_kwargs: {
        "subject": "subject", "text_body": "dynamic body", "html_body": None, "summary": {}
    })
    monkeypatch.setattr(module.sender, "_resolve_token", lambda: None)
    captured = {}

    def fake_fallback(target, subject, note, text_body):
        captured.update(target=target, subject=subject, text=text_body)
        return True, None

    monkeypatch.setattr(module.sender, "_send_hermes_fallback", fake_fallback)
    assert module.main(["--scheduled-slot", "2026-08-03T12:00:00Z"]) == 0
    assert captured == {
        "target": "slack:D0BA2PM9CFM", "subject": "subject", "text": "dynamic body"
    }

    for forbidden in ("--to", "--fallback-to", "--body-file", "--html-file"):
        try:
            module.main(["--scheduled-slot", "2026-08-03T12:00:00Z", forbidden, "attacker"])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"digest accepted forbidden option {forbidden}")


def test_failing_governed_folded_check_is_included_in_digest(monkeypatch):
    module = _load("fleet_health_digest_checks_test", DIGEST)
    monkeypatch.setattr(module.builder, "compose_report", lambda **_kwargs: {
        "subject": "subject", "text_body": "base digest", "html_body": "<p>base digest</p>", "summary": {}
    })
    monkeypatch.setattr(module, "run_folded_checks", lambda: [
        module.CheckResult("supabase-rls-guard", 2, "exposure detected", "")
    ])
    monkeypatch.setattr(module.sender, "_resolve_token", lambda: "token")
    captured = {}
    monkeypatch.setattr(module.sender, "_send_postmark", lambda _token, _args, text, html: (
        captured.update(text=text, html=html) or True, "id", None
    ))

    assert module.main(["--scheduled-slot", "2026-08-03T12:00:00Z"]) == 0
    assert "Consolidated health checks" in captured["text"]
    assert "supabase-rls-guard" in captured["text"]
    assert "exposure detected" in captured["text"]
    assert "supabase-rls-guard" in captured["html"]


def test_clean_folded_checks_do_not_bloat_digest(monkeypatch):
    module = _load("fleet_health_digest_clean_checks_test", DIGEST)
    monkeypatch.setattr(module, "run_folded_checks", lambda: [
        module.CheckResult("delivery-probe", 0, '{"status":"ok"}', "")
    ])
    assert module.render_check_findings(module.run_folded_checks()) == ("", "")


def test_delivery_probe_direct_check_reports_fresh_success(monkeypatch, tmp_path, capsys):
    path = SCRIPTS / "hermes_self_report_delivery_probe.py"
    module = _load("delivery_probe_direct_test", path)
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "ts": module.datetime.datetime.now(module.datetime.timezone.utc).isoformat(),
        "status": "sent",
        "channel": "postmark",
    }), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(path), "--receipt-path", str(receipt)])
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_usage_alert_direct_probe_is_pure_and_clean(monkeypatch, tmp_path, capsys):
    path = SCRIPTS / "hermes_usage_alert.py"
    module = _load("usage_alert_direct_test", path)
    jobs = tmp_path / "jobs.json"
    jobs.write_text('{"jobs": []}\n', encoding="utf-8")
    monkeypatch.setattr(module, "LOGS", [str(tmp_path / "missing-agent.log")])
    monkeypatch.setattr(module, "JOBS_PATH", str(jobs))
    # Isolate from the real dev/mini auth.json — see hermes_usage_alert.py's
    # own test fixture note on AUTH_PATH (86e2mb8p5).
    monkeypatch.setattr(module, "AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(module, "STATE_PATH", str(tmp_path / "must-not-write-state.json"))
    monkeypatch.setattr(module, "RECEIPT_PATH", str(tmp_path / "must-not-write-receipt.json"))
    monkeypatch.setattr(sys, "argv", [str(path), "--probe"])
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == {"events": [], "status": "clean"}
    assert not Path(module.STATE_PATH).exists() and not Path(module.RECEIPT_PATH).exists()


def test_validate_size_direct_probe_reports_clean_without_writes(monkeypatch, tmp_path, capsys):
    path = SCRIPTS / "hermes_validate_size_monitor.py"
    module = _load("validate_size_direct_test", path)
    monkeypatch.setattr(module, "SKILLS_ROOT", tmp_path / "skills")
    monkeypatch.setattr(module, "JOBS_PATH", tmp_path / "jobs.json")
    monkeypatch.setattr(module, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(sys, "argv", [str(path), "--probe"])
    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "clean"
    assert list(tmp_path.rglob("*")) == []


def test_model_deprecation_direct_check_returns_checker_json(monkeypatch, tmp_path, capsys):
    path = SCRIPTS / "ignite_model_deprecation_check.py"
    module = _load("model_deprecation_direct_test", path)
    checker = tmp_path / "ignite-state" / "scripts" / "model-deprecation-check.mjs"
    checker.parent.mkdir(parents=True)
    checker.write_text("// fixture\n", encoding="utf-8")
    monkeypatch.setenv("IGNITE_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"status":"clean"}\n', stderr=""
    ))
    assert module.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "clean"}


def test_supabase_rls_direct_probe_reports_clean_without_state_or_delivery(monkeypatch, tmp_path, capsys):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load("supabase_rls_direct_test", path)
    monkeypatch.setattr(module, "STATE_PATH", str(tmp_path / "must-not-write-state.json"))
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "fixture-token")
    monkeypatch.setattr(module, "_projects", lambda _token: [{"id": "project-1", "status": "ACTIVE_HEALTHY"}])
    monkeypatch.setattr(module, "_exposures", lambda _ref, _token: {})
    monkeypatch.setattr(module, "_send_slack", lambda _message: (_ for _ in ()).throw(
        AssertionError("probe must not deliver")
    ))
    monkeypatch.setattr(sys, "argv", [str(path), "--probe"])
    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"exposed": 0, "exposures": {}, "projects": 1, "status": "clean"}
    assert not Path(module.STATE_PATH).exists()


def test_supabase_rls_probe_fails_closed_when_project_list_request_fails(monkeypatch, capsys):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load("supabase_rls_project_failure_test", path)
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "fixture-token")
    monkeypatch.setattr(module, "_api_get", lambda _path, _token: None)
    monkeypatch.setattr(sys, "argv", [str(path), "--probe"])

    assert module.main() != 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert "projects" in result["reason"]


def test_supabase_rls_probe_fails_closed_on_one_project_advisor_failure(monkeypatch, capsys):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load("supabase_rls_advisor_failure_test", path)
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "fixture-token")
    monkeypatch.setattr(module, "_projects", lambda _token: [
        {"id": "project-ok", "status": "ACTIVE_HEALTHY"},
        {"id": "project-bad", "status": "ACTIVE_HEALTHY"},
    ])
    monkeypatch.setattr(module, "_exposures", lambda ref, _token: (
        {} if ref == "project-ok" else (_ for _ in ()).throw(module.ProbeError("advisor malformed"))
    ))
    monkeypatch.setattr(sys, "argv", [str(path), "--probe"])

    assert module.main() != 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert result["projects"] == 2
    assert result["errors"] == [{"project": "project-bad", "reason": "advisor malformed"}]


def test_supabase_rls_advisor_rejects_malformed_response(monkeypatch):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load("supabase_rls_malformed_test", path)
    monkeypatch.setattr(module, "_api_get", lambda _path, _token: {"unexpected": []})

    with pytest.raises(module.ProbeError, match="malformed advisor response"):
        module._exposures("project-1", "fixture-token")


@pytest.mark.parametrize("project", [
    {"id": "project-1"},
    {"id": "project-1", "status": None},
    {"id": "project-1", "status": 7},
    {"id": "project-1", "status": "ACTIVE_MYSTERY"},
])
def test_supabase_project_list_rejects_missing_non_string_or_unknown_status(monkeypatch, project):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load(f"supabase_project_status_{id(project)}", path)
    monkeypatch.setattr(module, "_api_get", lambda _path, _token: [project])

    with pytest.raises(module.ProbeError, match="malformed projects response.*status"):
        module._projects("fixture-token")


def test_supabase_malformed_project_status_exits_nonzero_for_digest(monkeypatch, capsys):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load("supabase_malformed_status_digest_guard", path)
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "fixture-token")
    monkeypatch.setattr(module, "_api_get", lambda _path, _token: [{"id": "project-1"}])
    monkeypatch.setattr(sys, "argv", [str(path), "--probe"])

    assert module.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert "status" in result["reason"]


def test_supabase_rls_probe_fails_closed_without_auth(monkeypatch, capsys):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load("supabase_rls_auth_failure_test", path)
    monkeypatch.delenv("SUPABASE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", [str(path), "--probe"])

    assert module.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "error", "reason": "SUPABASE_ACCESS_TOKEN unavailable"
    }


def test_supabase_rls_network_failure_has_durable_endpoint_reason(monkeypatch):
    path = SCRIPTS / "supabase_rls_guard.py"
    module = _load("supabase_rls_network_failure_test", path)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(module.urllib.error.URLError("offline")))

    with pytest.raises(module.ProbeError, match=r"GET /v1/projects failed:.*offline"):
        module._api_get("/v1/projects", "fixture-token")


def test_supabase_error_exit_is_preserved_as_failed_folded_check(monkeypatch):
    module = _load("fleet_health_digest_supabase_fold_test", DIGEST)

    def fake_run(argv, **_kwargs):
        if str(argv[2]).endswith("supabase_rls_guard.py"):
            return SimpleNamespace(
                returncode=2,
                stdout='{"status":"error","reason":"projects request failed"}',
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout='{"status":"clean"}', stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = next(item for item in module.run_folded_checks() if item.name == "supabase-rls-guard")
    assert result.returncode == 2
    assert '"status":"error"' in result.stdout
