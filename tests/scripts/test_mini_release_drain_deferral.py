"""Release-cut drain deferral vs failure (ClickUp 86e2md9ck).

A release cut arms the gateway's reversible external drain and waits for
``gateway_state=draining`` + ``active_agents=0`` before switching the runtime.
A normal cron agent run is 25-45 minutes, so that window routinely expires while
the fleet is simply busy.  Before this change the expiry was treated as a
release FAILURE: the EXIT trap froze poll-control, and the poller then refused
to try again until an operator re-certified with a fresh promotion receipt --
so a busy fleet could lock itself out of its own deploy path.

These tests pin the two halves of the fix and, deliberately, the safety
property it must not erode:

* a drain timeout that left nothing armed and nothing switched is a DEFERRAL --
  exit 75 (EX_TEMPFAIL), poll-control untouched, poller retries unattended;
* a genuine failure, and a drain marker that could not be cleared, still FREEZE;
* the switch is still refused whenever agents are actually active.

They exercise the real shell, not a transcription of it: the cutter is sourced
in its documented library mode and the non-function decision blocks are
extracted from the live script text, so a future edit that drops the deferral
branch (or re-freezes on it) fails here.

Lives in the main CI python lane on purpose -- ``tests/scripts/`` runs on every
PR, while ``tests/scripts/test_mini_release_cut_safety.sh`` currently has no CI
caller at all.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CUT_SCRIPT = REPO_ROOT / "scripts" / "mini-release-cut.sh"
POLL_SCRIPT = REPO_ROOT / "scripts" / "mini-release-poll.sh"
MINI_SCRIPTS = REPO_ROOT / "machine-setup" / "mini-scripts"
CONTRACTS_PATH = MINI_SCRIPTS / "fleet_outcome_contracts.json"
MANIFEST_PATH = MINI_SCRIPTS / "fleet_outcome_manifest.json"

DEFER_EXIT = 75

# The literal the cutter warns with on a deferral. The fleet-outcome contract
# counts this exact text out of the poll log, so the two must not drift apart.
DEFER_LOG_TEXT = "release cut deferred: gateway did not quiesce"


def _run_bash(body: str, *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script = cwd / "harness.sh"
    script.write_text(body, encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("MINI_RELEASE_DRAIN_TIMEOUT", None)
    environment.pop("MINI_RELEASE_VERIFY_TIMEOUT", None)
    environment.pop("MINI_RELEASE_KEEP_EXTRA", None)
    if env:
        environment.update(env)
    return subprocess.run(
        ["bash", str(script)],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture()
def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".hermes"
    (home / "releases").mkdir(parents=True)
    return home


def _lib_preamble(hermes_home: Path) -> str:
    """Source the cutter in its documented, non-mutating library mode."""
    return (
        "set -uo pipefail\n"
        f'export HERMES_HOME="{hermes_home}"\n'
        f'MINI_RELEASE_CUT_TEST_LIB=1 source "{CUT_SCRIPT}"\n'
    )


def _cleanup_body() -> str:
    """Extract the real EXIT trap so the test cannot drift from the script."""
    text = CUT_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^cleanup_on_exit\(\) \{.*?^\}$", text, re.DOTALL | re.MULTILINE)
    assert match, "cleanup_on_exit could not be extracted from mini-release-cut.sh"
    return match.group(0)


def _drain_decision_body() -> str:
    """Extract the real post-drain decision block (deferral vs die)."""
    text = CUT_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"^DRAIN_STATUS=0$.*?^fi$",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "post-drain decision block could not be extracted from mini-release-cut.sh"
    return match.group(0)


# ---------------------------------------------------------------------------
# The wait itself
# ---------------------------------------------------------------------------


def test_drain_timeout_returns_the_deferral_exit_code(tmp_path: Path, hermes_home: Path) -> None:
    """A busy fleet must produce 75, not the generic failure 1."""
    body = _lib_preamble(hermes_home) + f"""
DRY_RUN=0
DRAIN_TIMEOUT=3
LEASE_HEARTBEAT_INTERVAL=1
SECONDS=0
DRAIN_REQUEST_FILE="$HERMES_HOME/.drain_request.json"
GATEWAY_STATE_FILE="$HERMES_HOME/gateway_state.json"
heartbeat_production_write_lease() {{ :; }}
sleep() {{
  SECONDS=$((SECONDS + $1))
  printf '{{"gateway_state":"draining","active_agents":3}}\\n' > "$GATEWAY_STATE_FILE"
}}
printf '{{"action":"drain"}}\\n' > "$DRAIN_REQUEST_FILE"
status=0
wait_for_release_drain || status=$?
printf 'WAIT_STATUS=%s\\n' "$status"
printf 'DECLARED_DEFER_EXIT=%s\\n' "$RELEASE_DEFER_EXIT"
"""
    result = _run_bash(body, cwd=tmp_path)
    assert "WAIT_STATUS=75" in result.stdout, result.stdout + result.stderr
    assert "DECLARED_DEFER_EXIT=75" in result.stdout
    assert "did not drain within" in result.stderr


def test_refuses_switch_while_agents_active(tmp_path: Path, hermes_home: Path) -> None:
    """SAFETY: quiescence is never certified while work is in flight.

    This is the property the deferral change must not weaken. Every unsafe
    aggregate status -- agents still running, a gateway that never acknowledged
    the drain, and a stale status file written before this cutter's marker --
    must be rejected, and the bounded wait must never report success for them.
    """
    body = _lib_preamble(hermes_home) + f"""
DRY_RUN=0
DRAIN_TIMEOUT=2
LEASE_HEARTBEAT_INTERVAL=1
DRAIN_REQUEST_FILE="$HERMES_HOME/.drain_request.json"
GATEWAY_STATE_FILE="$HERMES_HOME/gateway_state.json"
heartbeat_production_write_lease() {{ :; }}

check() {{
  local label="$1" payload="$2" stale="${{3:-0}}"
  printf '{{"action":"drain"}}\\n' > "$DRAIN_REQUEST_FILE"
  if [ "$stale" = 1 ]; then
    printf '%s\\n' "$payload" > "$GATEWAY_STATE_FILE"
    touch -t 200001010000 "$GATEWAY_STATE_FILE"
  else
    printf '%s\\n' "$payload" > "$GATEWAY_STATE_FILE"
  fi
  if release_drain_quiesced; then
    printf 'QUIESCED_%s=yes\\n' "$label"
  else
    printf 'QUIESCED_%s=no\\n' "$label"
  fi
}}

check ACTIVE_AGENTS '{{"gateway_state":"draining","active_agents":1}}'
check NOT_DRAINING '{{"gateway_state":"running","active_agents":0}}'
check STALE_STATUS '{{"gateway_state":"draining","active_agents":0}}' 1
check CLEAN '{{"gateway_state":"draining","active_agents":0}}'

# And the bounded wait never reports success while agents stay active.
SECONDS=0
printf '{{"action":"drain"}}\\n' > "$DRAIN_REQUEST_FILE"
sleep() {{
  SECONDS=$((SECONDS + $1))
  printf '{{"gateway_state":"draining","active_agents":4}}\\n' > "$GATEWAY_STATE_FILE"
}}
wait_status=0
wait_for_release_drain || wait_status=$?
printf 'BUSY_WAIT_STATUS=%s\\n' "$wait_status"
"""
    result = _run_bash(body, cwd=tmp_path)
    out = result.stdout
    assert "QUIESCED_ACTIVE_AGENTS=no" in out, out + result.stderr
    assert "QUIESCED_NOT_DRAINING=no" in out, out + result.stderr
    assert "QUIESCED_STALE_STATUS=no" in out, out + result.stderr
    # Positive control: the gate is satisfiable, so "no" above is a real refusal
    # and not an unconditionally-red check.
    assert "QUIESCED_CLEAN=yes" in out, out + result.stderr
    # Never 0: a busy fleet is never allowed to authorize the runtime switch.
    assert "BUSY_WAIT_STATUS=75" in out, out + result.stderr


# ---------------------------------------------------------------------------
# The decision block: deferral vs failure
# ---------------------------------------------------------------------------


def _decision_harness(hermes_home: Path, *, drain_status: int, clear_ok: bool) -> str:
    return _lib_preamble(hermes_home) + f"""
DRY_RUN=0
DRAIN_TIMEOUT=300
RELEASE_DRAIN_ARMED=1
begin_release_drain() {{ return {drain_status}; }}
clear_release_drain() {{
  if [ "{int(clear_ok)}" = 1 ]; then RELEASE_DRAIN_ARMED=0; return 0; fi
  return 1
}}
die() {{ printf 'DIED: %s\\n' "$*" >&2; exit 1; }}
{_drain_decision_body()}
printf 'REACHED_SWITCH=yes\\n'
printf 'RELEASE_DEFERRED=%s\\n' "$RELEASE_DEFERRED"
"""


def test_busy_fleet_defers_without_reaching_the_switch(tmp_path: Path, hermes_home: Path) -> None:
    result = _run_bash(_decision_harness(hermes_home, drain_status=DEFER_EXIT, clear_ok=True), cwd=tmp_path)
    assert result.returncode == DEFER_EXIT, (result.returncode, result.stdout, result.stderr)
    assert "REACHED_SWITCH" not in result.stdout, result.stdout
    assert DEFER_LOG_TEXT in result.stderr, result.stderr
    assert "DIED:" not in result.stderr


def test_drain_arm_failure_is_still_a_hard_failure(tmp_path: Path, hermes_home: Path) -> None:
    """Failing to ARM the drain is an operational fault, not a busy fleet."""
    result = _run_bash(_decision_harness(hermes_home, drain_status=1, clear_ok=True), cwd=tmp_path)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "DIED: cut aborted before runtime switch" in result.stderr
    assert DEFER_LOG_TEXT not in result.stderr


def test_unclearable_marker_is_never_treated_as_a_deferral(tmp_path: Path, hermes_home: Path) -> None:
    """A marker stuck on parks the whole fleet in drain -- that is a real fault."""
    result = _run_bash(_decision_harness(hermes_home, drain_status=DEFER_EXIT, clear_ok=False), cwd=tmp_path)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "DIED: cut aborted before runtime switch" in result.stderr
    assert DEFER_LOG_TEXT not in result.stderr


# ---------------------------------------------------------------------------
# The EXIT trap: who freezes poll-control
# ---------------------------------------------------------------------------


def _cleanup_harness(
    hermes_home: Path,
    marker: Path,
    *,
    deferred: int,
    exit_status: int,
    drain_armed: int = 0,
    clear_ok: bool = True,
) -> str:
    return _lib_preamble(hermes_home) + f"""
{_cleanup_body()}
DRY_RUN=0
NEW_DIR=""
LEASE_CUT_READY=1
RELEASE_DEFERRED={deferred}
RELEASE_DRAIN_ARMED={drain_armed}
production_write_mutation_allowed() {{ return 0; }}
clear_release_drain() {{
  if [ "{int(clear_ok)}" = 1 ]; then RELEASE_DRAIN_ARMED=0; return 0; fi
  return 1
}}
freeze_managed_poll_after_failure() {{ printf 'froze\\n' > "{marker}"; return 0; }}
release_cut_lock() {{ return 0; }}
release_production_write_lease() {{ return 0; }}
cleanup_production_write_lease_bootstrap() {{ :; }}
# Sourcing the cutter turns on `set -e`; the trap normally runs *because* of it,
# so drop it here to hand cleanup_on_exit a simulated $? the same way.
set +e
( exit {exit_status} )
cleanup_on_exit
"""


def test_deferred_exit_does_not_freeze_poll_control(tmp_path: Path, hermes_home: Path) -> None:
    """The core fix: a deferral leaves the poller authorized to retry."""
    marker = tmp_path / "froze"
    result = _run_bash(
        _cleanup_harness(hermes_home, marker, deferred=1, exit_status=DEFER_EXIT),
        cwd=tmp_path,
    )
    assert not marker.exists(), "a deferral froze poll-control: " + result.stderr
    assert result.returncode == DEFER_EXIT, (result.returncode, result.stderr)
    assert "poll freeze skipped: release deferred, not failed" in result.stderr


def test_genuine_failure_still_freezes_poll_control(tmp_path: Path, hermes_home: Path) -> None:
    """SAFETY: build/verify failures keep their fail-closed operator gate."""
    marker = tmp_path / "froze"
    result = _run_bash(
        _cleanup_harness(hermes_home, marker, deferred=0, exit_status=1),
        cwd=tmp_path,
    )
    assert marker.exists(), "a genuine failure did not freeze poll-control: " + result.stderr
    assert result.returncode == 1, (result.returncode, result.stderr)


def test_deferral_claim_is_revoked_when_the_marker_cannot_be_cleared(
    tmp_path: Path, hermes_home: Path
) -> None:
    marker = tmp_path / "froze"
    result = _run_bash(
        _cleanup_harness(
            hermes_home,
            marker,
            deferred=1,
            exit_status=DEFER_EXIT,
            drain_armed=1,
            clear_ok=False,
        ),
        cwd=tmp_path,
    )
    assert marker.exists(), "stuck drain marker skipped the freeze: " + result.stderr
    assert result.returncode == 70, (result.returncode, result.stderr)


def test_successful_cut_never_freezes(tmp_path: Path, hermes_home: Path) -> None:
    marker = tmp_path / "froze"
    result = _run_bash(
        _cleanup_harness(hermes_home, marker, deferred=0, exit_status=0),
        cwd=tmp_path,
    )
    assert not marker.exists()
    assert result.returncode == 0, (result.returncode, result.stderr)


# ---------------------------------------------------------------------------
# The poller wrapper
# ---------------------------------------------------------------------------


def _poll_tail_body() -> str:
    text = POLL_SCRIPT.read_text(encoding="utf-8")
    index = text.index("CUT_STATUS=0")
    return text[index:]


def _poll_harness(tmp_path: Path, cut_exit: int) -> str:
    stub = tmp_path / "cut-stub.sh"
    stub.write_text(f'#!/usr/bin/env bash\nexit {cut_exit}\n', encoding="utf-8")
    stub.chmod(0o755)
    return (
        "set -uo pipefail\n"
        f'CUT="{stub}"\n'
        'CERTIFIED_SHA="0000000000000000000000000000000000000000"\n'
        'RECEIPT_ID="' + "0" * 64 + '"\n'
        + _poll_tail_body()
    )


def test_poller_treats_the_deferral_exit_as_a_clean_retry(tmp_path: Path) -> None:
    result = _run_bash(_poll_harness(tmp_path, DEFER_EXIT), cwd=tmp_path)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "cut deferred by a busy fleet" in result.stdout
    # The poller's own line must not also match the counted contract pattern,
    # or one deferral would be counted twice toward the alarm threshold.
    assert DEFER_LOG_TEXT not in result.stdout


@pytest.mark.parametrize("code", [1, 2, 70])
def test_poller_propagates_real_cut_failures(tmp_path: Path, code: int) -> None:
    result = _run_bash(_poll_harness(tmp_path, code), cwd=tmp_path)
    assert result.returncode == code, (result.returncode, result.stdout, result.stderr)
    assert "cut deferred" not in result.stdout


def test_poller_and_cutter_agree_on_the_deferral_code() -> None:
    """The wrapper hard-codes 75; keep it welded to the cutter's constant."""
    cutter = CUT_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^RELEASE_DEFER_EXIT=(\d+)$", cutter, re.MULTILINE)
    assert match, "mini-release-cut.sh no longer declares RELEASE_DEFER_EXIT"
    assert int(match.group(1)) == DEFER_EXIT
    poller = POLL_SCRIPT.read_text(encoding="utf-8")
    assert f'"$CUT_STATUS" -eq {DEFER_EXIT}' in poller, (
        "mini-release-poll.sh no longer recognises the cutter's deferral exit code"
    )


def test_poller_reports_success_unchanged(tmp_path: Path) -> None:
    result = _run_bash(_poll_harness(tmp_path, 0), cwd=tmp_path)
    assert result.returncode == 0
    assert "cut deferred" not in result.stdout


# ---------------------------------------------------------------------------
# Visibility: a fleet that can NEVER quiesce must alarm, not go quiet
# ---------------------------------------------------------------------------


def _deferral_contract() -> dict:
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    matches = [
        item
        for item in contracts["operational_checks"]
        if item.get("id") == "release-cut-drain-deferral"
    ]
    assert len(matches) == 1, "exactly one release-cut-drain-deferral contract must be wired"
    return matches[0]


def test_repeated_deferrals_are_wired_to_a_bounded_alarm() -> None:
    contract = _deferral_contract()
    assert contract["kind"] == "repeated_release_drift"
    assert contract["path"] == "~/.hermes/logs/mini-release-poll.log"
    assert contract["threshold"] >= 2, "a single deferral must not page"
    # Without a recency window the byte-window tail counts resolved history
    # forever and the alarm becomes permanently red.
    assert contract["window_minutes"] > 0
    assert contract["timestamp_pattern"]


def test_contract_pattern_matches_the_line_the_cutter_actually_emits() -> None:
    """The alarm is worthless if the pattern and the log text drift apart."""
    contract = _deferral_contract()
    source = CUT_SCRIPT.read_text(encoding="utf-8")
    emitted = [line for line in source.splitlines() if DEFER_LOG_TEXT in line and "warn " in line]
    assert len(emitted) == 1, "expected exactly one deferral warn() in mini-release-cut.sh"
    # Render the shell literal the way the cutter would at runtime.
    rendered = emitted[0].split('warn "', 1)[1].rsplit('"', 1)[0].replace("${DRAIN_TIMEOUT}", "300")
    assert re.search(contract["pattern"], rendered, re.IGNORECASE), (
        f"contract pattern {contract['pattern']!r} does not match emitted line {rendered!r}"
    )


def test_contracts_manifest_pin_is_current() -> None:
    """An unpinned contracts edit silently rolls back every release cut."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entry = next(
        item for item in manifest["files"] if item["source"] == "fleet_outcome_contracts.json"
    )
    actual = hashlib.sha256(CONTRACTS_PATH.read_bytes()).hexdigest()
    assert entry["sha256"] == actual, (
        "fleet_outcome_manifest sha256 for fleet_outcome_contracts.json is stale"
    )
