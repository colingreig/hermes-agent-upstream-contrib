"""writer-served.jsonl must record SUBSCRIPTION-ROUTED cost, not OpenCode's raw cost.

86e2hap1g part 2. The spend cap/alert path (spend_guard/spend_meter) was fixed to
route OpenCode log costs through resolve_billing_route(), but opencode_exec's
_record_served still wrote the RAW, unrouted ``part.cost`` into
~/.hermes/logs/writer-served.jsonl. hermes_report_build{,_v2}.py sum that field
for the daily status email's spend total and per-provider breakdown, so every
Codex-OAuth-proxied run still showed phantom spend in the digest.

The bar these tests hold: a PROVEN Codex-OAuth route records $0, and anything
unproven or non-codex keeps its FULL recorded cost (no blanket zeroing).
"""
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MINI_SCRIPTS = ROOT / "machine-setup" / "mini-scripts"


def _load_script(name):
    if str(MINI_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(MINI_SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, MINI_SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_opencode_config(home, base_url):
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "opencode.jsonc").write_text(
        json.dumps({"provider": {"openai": {"options": {"baseURL": base_url}}}}),
        encoding="utf-8",
    )


def _served_rows(ledger):
    return [json.loads(line) for line in Path(ledger).read_text(encoding="utf-8").splitlines() if line.strip()]


def _record(tmp_path, monkeypatch, model, cost, proxy_base_url=None):
    """Run _record_served for one result and return (result, ledger_rows)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    if proxy_base_url:
        _write_opencode_config(tmp_path, proxy_base_url)
    opencode_exec = _load_script("opencode_exec")
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(opencode_exec, "_LIVENESS_LEDGER", str(ledger))
    result = {"task_id": "86e2hap1g", "model": model, "cost_usd": cost, "ok": True}
    opencode_exec._record_served(result, True)
    return result, _served_rows(ledger)


def test_codex_oauth_served_row_records_zero_cost(tmp_path, monkeypatch):
    """Codex OAuth through the PROVEN local proxy is subscription-included → $0."""
    result, rows = _record(
        tmp_path, monkeypatch, "openai/gpt-5.5", 49.50, proxy_base_url="http://127.0.0.1:8646/v1"
    )

    assert len(rows) == 1
    assert rows[0]["cost_usd"] == 0.0
    assert rows[0]["raw_cost_usd"] == 49.50  # raw figure preserved, never lost
    assert rows[0]["billing_mode"] == "subscription_included"
    assert rows[0]["billing_provider"] == "openai-codex"
    assert result["cost_usd"] == 0.0
    assert result["raw_cost_usd"] == 49.50


def test_unproven_openai_served_row_remains_fully_billed(tmp_path, monkeypatch):
    """No proven codex proxy configured → the SAME model still bills in full."""
    result, rows = _record(
        tmp_path, monkeypatch, "openai/gpt-5.5", 8.25, proxy_base_url="https://api.openai.com/v1"
    )

    assert rows[0]["cost_usd"] == 8.25
    assert rows[0]["raw_cost_usd"] == 8.25
    assert rows[0]["billing_mode"] != "subscription_included"
    assert result["cost_usd"] == 8.25


def test_genuinely_billed_provider_served_row_keeps_full_cost(tmp_path, monkeypatch):
    """A real metered provider (anthropic) is untouched — no blanket zeroing."""
    _result, rows = _record(
        tmp_path, monkeypatch, "anthropic/claude-sonnet-5", 2.50, proxy_base_url="http://127.0.0.1:8646/v1"
    )

    assert rows[0]["cost_usd"] == 2.50
    assert rows[0]["billing_provider"] == "anthropic"
    assert rows[0]["billing_mode"] != "subscription_included"


def test_missing_cost_stays_null_and_still_records_liveness(tmp_path, monkeypatch):
    """A run with no reported cost must still write its served-tier row."""
    _result, rows = _record(
        tmp_path, monkeypatch, "openai/gpt-5.5", None, proxy_base_url="http://127.0.0.1:8646/v1"
    )

    assert len(rows) == 1
    assert rows[0]["cost_usd"] is None
    assert rows[0]["served_model"] == "openai/gpt-5.5"


def test_repeated_record_served_is_idempotent(tmp_path, monkeypatch):
    """_record_served runs at 5 exit paths; re-routing must not consume the raw cost."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_opencode_config(tmp_path, "https://api.openai.com/v1")
    opencode_exec = _load_script("opencode_exec")
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(opencode_exec, "_LIVENESS_LEDGER", str(ledger))
    result = {"task_id": "86e2hap1g", "model": "openai/gpt-5.5", "cost_usd": 8.25, "ok": True}

    opencode_exec._record_served(result, True)
    opencode_exec._record_served(result, True)

    rows = _served_rows(ledger)
    assert [r["cost_usd"] for r in rows] == [8.25, 8.25]
    assert [r["raw_cost_usd"] for r in rows] == [8.25, 8.25]


def test_billing_route_failure_falls_open_to_the_raw_cost(tmp_path, monkeypatch):
    """An unresolvable route must never silently zero real spend."""
    monkeypatch.setenv("HOME", str(tmp_path))
    opencode_exec = _load_script("opencode_exec")
    ledger = tmp_path / "logs" / "writer-served.jsonl"
    monkeypatch.setattr(opencode_exec, "_LIVENESS_LEDGER", str(ledger))
    monkeypatch.setattr(
        opencode_exec, "_billing_route_for_model",
        lambda _model: (_ for _ in ()).throw(RuntimeError("route resolver down")),
    )
    result = {"task_id": "86e2hap1g", "model": "openai/gpt-5.5", "cost_usd": 12.00, "ok": True}

    opencode_exec._record_served(result, True)

    rows = _served_rows(ledger)
    assert rows[0]["cost_usd"] == 12.00
    assert rows[0]["billing_mode"] == "unknown"


# ── status-email read path ────────────────────────────────────────────────────
# Rows already on disk pre-date the write-site fix, so the digest normalizes them
# at read time through the same shared resolver rather than rewriting the ledger.

def _legacy_row(model, provider, cost):
    return {"served_model": model, "served_provider": provider, "cost_usd": cost}


def test_served_row_cost_zeroes_legacy_codex_rows(tmp_path, monkeypatch):
    spend_opencode = _load_script("spend_opencode")
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_opencode_config(tmp_path, "http://127.0.0.1:8647/v1")

    assert spend_opencode.served_row_cost(_legacy_row("openai/gpt-5.5", "openai-codex", 49.50)) == 0.0


def test_served_row_cost_keeps_legacy_billed_rows(tmp_path, monkeypatch):
    spend_opencode = _load_script("spend_opencode")
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_opencode_config(tmp_path, "http://127.0.0.1:8647/v1")

    assert spend_opencode.served_row_cost(_legacy_row("anthropic/claude-sonnet-5", "anthropic", 2.50)) == 2.50
    assert spend_opencode.served_row_cost(_legacy_row("zai-coding/glm-4.7", "zai", 1.10)) == 1.10


def test_served_row_cost_bills_legacy_codex_rows_without_a_proven_proxy(tmp_path, monkeypatch):
    """No proven local proxy → an "openai-codex" label alone proves nothing."""
    spend_opencode = _load_script("spend_opencode")
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_opencode_config(tmp_path, "https://api.openai.com/v1")

    assert spend_opencode.served_row_cost(_legacy_row("openai/gpt-5.5", "openai-codex", 49.50)) == 49.50


def test_served_row_cost_trusts_already_routed_rows(tmp_path, monkeypatch):
    """A post-fix row is authoritative — do NOT re-route it."""
    spend_opencode = _load_script("spend_opencode")
    monkeypatch.setenv("HOME", str(tmp_path))
    row = {
        "served_model": "anthropic/claude-sonnet-5",
        "served_provider": "anthropic",
        "cost_usd": 2.50,
        "raw_cost_usd": 2.50,
        "billing_mode": "official_docs_snapshot",
    }

    assert spend_opencode.served_row_cost(row) == 2.50
    assert spend_opencode.served_row_cost({**row, "cost_usd": None}) == 0.0


def test_status_email_spend_summary_drops_phantom_codex_spend(tmp_path, monkeypatch):
    report = _load_script("hermes_report_build")
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_opencode_config(tmp_path, "http://127.0.0.1:8646/v1")
    rows = [
        _legacy_row("openai/gpt-5.5", "openai-codex", 49.50),      # phantom
        _legacy_row("anthropic/claude-sonnet-5", "anthropic", 2.50),  # real
    ]

    spend = report.summarize_spend(rows, rows, [])

    assert spend["total_cost"] == 2.50
    assert spend["today_cost"] == 2.50
    by_provider = {r["provider"]: r["cost"] for r in spend["provider_rows"]}
    assert by_provider["openai-codex"] == 0.0
    assert by_provider["anthropic"] == 2.50


def test_status_email_v2_spend_summary_drops_phantom_codex_spend(tmp_path, monkeypatch):
    report_v2 = _load_script("hermes_report_build_v2")
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_opencode_config(tmp_path, "http://127.0.0.1:8646/v1")
    rows = [
        _legacy_row("openai/gpt-5.4-mini", "openai-codex", 60.00),
        _legacy_row("anthropic/claude-sonnet-5", "anthropic", 1.25),
    ]

    spend = report_v2.summarize_spend(rows, [], [])

    assert spend["total_cost"] == 1.25
