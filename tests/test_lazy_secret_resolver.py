"""Tests for `agent.lazy_secret_resolver`.

These are hermetic: the `onepassword` SDK call is never exercised directly.
Instead, the tests monkeypatch the module's `_resolve_ref` seam — the single
boundary between the cache/manifest logic and the actual SDK — so the suite
passes whether or not the `onepassword` package is installed and never
touches the network.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Optional

import pytest

# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import lazy_secret_resolver as lsr  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Every test starts with a clean manifest/value cache."""
    lsr.clear_cache()
    yield
    lsr.clear_cache()


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    """Write a small manifest and point the module at it."""
    manifest_path = tmp_path / "op-secrets.env"
    manifest_path.write_text(
        "FOO_KEY=op://vault/item-foo/field\n"
        "BAR_KEY=op://vault/item-bar/field\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_OP_SECRETS_MANIFEST", str(manifest_path))
    return manifest_path


def test_get_known_name_returns_resolved_value(manifest, monkeypatch):
    calls = []

    def fake_resolve_ref(ref: str) -> Optional[str]:
        calls.append(ref)
        return "sk-super-secret"

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    assert lsr.get("FOO_KEY") == "sk-super-secret"
    assert calls == ["op://vault/item-foo/field"]


def test_second_get_hits_cache_resolver_called_once(manifest, monkeypatch):
    calls = []

    def fake_resolve_ref(ref: str) -> Optional[str]:
        calls.append(ref)
        return "sk-super-secret"

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    first = lsr.get("FOO_KEY")
    second = lsr.get("FOO_KEY")

    assert first == "sk-super-secret"
    assert second == "sk-super-secret"
    assert len(calls) == 1


def test_ttl_expiry_triggers_re_resolve(manifest, monkeypatch):
    monkeypatch.setenv("HERMES_LAZY_SECRET_TTL", "0")

    calls = []

    def fake_resolve_ref(ref: str) -> Optional[str]:
        calls.append(ref)
        return f"value-{len(calls)}"

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    first = lsr.get("FOO_KEY")
    second = lsr.get("FOO_KEY")

    assert first == "value-1"
    # TTL of 0 means the cached entry's expiry (now + 0) is never strictly
    # greater than "now" on the next call, so it re-resolves immediately.
    assert second == "value-2"
    assert len(calls) == 2


def test_unknown_name_returns_none(manifest, monkeypatch):
    def fake_resolve_ref(ref: str) -> Optional[str]:
        raise AssertionError("should not be called for an unknown name")

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    assert lsr.get("NOT_IN_MANIFEST") is None


def test_missing_manifest_file_returns_none_no_raise(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist.env"
    monkeypatch.setenv("HERMES_OP_SECRETS_MANIFEST", str(missing_path))

    def fake_resolve_ref(ref: str) -> Optional[str]:
        raise AssertionError("should not be called when the manifest is missing")

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    assert lsr.get("ANYTHING") is None


def test_resolver_returning_none_propagates_none(manifest, monkeypatch):
    def fake_resolve_ref(ref: str) -> Optional[str]:
        return None

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    assert lsr.get("FOO_KEY") is None


def test_never_mutates_os_environ(manifest, monkeypatch):
    import os

    def fake_resolve_ref(ref: str) -> Optional[str]:
        return "sk-super-secret"

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    before = set(os.environ.keys())
    lsr.get("FOO_KEY")
    lsr.get("FOO_KEY")  # cache hit path too
    lsr.get("UNKNOWN")
    after = set(os.environ.keys())

    # HERMES_OP_SECRETS_MANIFEST/HERMES_LAZY_SECRET_TTL are set by monkeypatch
    # fixtures themselves (and torn down by monkeypatch, not by our code) —
    # what matters is that `get()` itself never adds/removes/mutates keys.
    assert before == after


def test_hung_resolution_for_one_ref_does_not_block_a_different_ref(manifest, monkeypatch):
    """Regression test for the concurrency wedge this module used to have.

    A hung SDK call resolving FOO_KEY's ref must never block a concurrent
    `get()` for BAR_KEY's (different) ref — there is no shared lock or
    shared executor spanning the two. FOO_KEY's own call must still give up
    and return None once the (short, test-configured) resolve timeout
    elapses, rather than hanging forever.
    """
    monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLVE_TIMEOUT", "1")

    hang_forever = threading.Event()  # intentionally never set

    def fake_resolve_ref(ref: str) -> Optional[str]:
        if ref == "op://vault/item-foo/field":
            # Simulates a wedged SDK call: blocks with no timeout of its
            # own. This runs on `_resolve_with_timeout`'s throwaway daemon
            # thread, not on the caller's thread.
            hang_forever.wait()
            return "should-never-be-returned"
        return "value-for-bar"

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    result_a: dict = {}

    def call_a() -> None:
        start = time.monotonic()
        result_a["value"] = lsr.get("FOO_KEY")
        result_a["elapsed"] = time.monotonic() - start

    thread_a = threading.Thread(target=call_a)
    thread_a.start()

    # Give A's call a moment to register itself as the in-flight leader for
    # its ref before we race B against it.
    time.sleep(0.1)

    start_b = time.monotonic()
    value_b = lsr.get("BAR_KEY")
    elapsed_b = time.monotonic() - start_b

    assert value_b == "value-for-bar"
    # B must complete promptly and well under A's 1s timeout — this is the
    # crux of the regression test: B is not queued behind A's hang.
    assert elapsed_b < 0.5, f"BAR_KEY resolution took {elapsed_b}s; A's hang leaked through"

    thread_a.join(timeout=5)
    assert not thread_a.is_alive(), "FOO_KEY's get() never returned"
    assert result_a["value"] is None
    # Bounded by (roughly) the configured timeout, not stuck forever.
    assert result_a["elapsed"] >= 1.0
    assert result_a["elapsed"] < 4.0


def test_concurrent_get_for_same_name_single_flights_the_resolver(manifest, monkeypatch):
    """Two concurrent misses for the SAME ref must trigger only one SDK call."""
    calls: list = []
    call_lock = threading.Lock()

    def fake_resolve_ref(ref: str) -> Optional[str]:
        with call_lock:
            calls.append(ref)
        time.sleep(0.3)
        return "sk-shared-secret"

    monkeypatch.setattr(lsr, "_resolve_ref", fake_resolve_ref)

    results = [None, None]

    def call_get(idx: int) -> None:
        results[idx] = lsr.get("FOO_KEY")

    t1 = threading.Thread(target=call_get, args=(0,))
    t2 = threading.Thread(target=call_get, args=(1,))
    t1.start()
    # Ensure t1 has registered itself as the single-flight leader before t2
    # starts, so t2 deterministically takes the follower path.
    time.sleep(0.05)
    t2.start()

    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert results[0] == "sk-shared-secret"
    assert results[1] == "sk-shared-secret"
    assert len(calls) == 1


def test_manifest_is_refreshed_after_atomic_replacement(manifest, monkeypatch):
    calls = []

    def fake_resolve_required(ref: str) -> str:
        calls.append(ref)
        return f"value-{len(calls)}"

    monkeypatch.setattr(lsr, "_resolve_ref_required", fake_resolve_required)
    assert lsr.get_required("FOO_KEY") == "value-1"

    replacement = manifest.with_suffix(".replacement")
    replacement.write_text(
        "FOO_KEY=op://vault/item-rotated/field\n",
        encoding="utf-8",
    )
    replacement.replace(manifest)

    assert lsr.get_required("FOO_KEY") == "value-2"
    assert calls == [
        "op://vault/item-foo/field",
        "op://vault/item-rotated/field",
    ]


def test_get_required_missing_is_typed_and_does_not_call_resolver(
    manifest, monkeypatch
):
    resolver = pytest.fail
    monkeypatch.setattr(lsr, "_resolve_ref_required", resolver)

    with pytest.raises(lsr.RequiredSecretMissingError) as excinfo:
        lsr.get_required("NOT_DECLARED")

    assert excinfo.value.classification == "missing"
    assert excinfo.value.name == "NOT_DECLARED"


def test_get_required_retries_only_transient_three_attempts(manifest, monkeypatch):
    calls = []
    sleeps = []

    def transient(_ref: str) -> str:
        calls.append("call")
        raise lsr.RequiredSecretTransientError("")

    monkeypatch.setattr(lsr, "_resolve_ref_required", transient)
    monkeypatch.setattr(lsr.time, "sleep", sleeps.append)

    with pytest.raises(lsr.RequiredSecretTransientError) as excinfo:
        lsr.get_required("FOO_KEY")

    assert excinfo.value.name == "FOO_KEY"
    assert len(calls) == 3
    assert sleeps == [0.2, 0.5]


@pytest.mark.parametrize(
    "error_type",
    [
        lsr.RequiredSecretMissingError,
        lsr.RequiredSecretAuthError,
        lsr.RequiredSecretFatalError,
    ],
)
def test_get_required_non_transient_error_is_not_retried(
    manifest, monkeypatch, error_type
):
    calls = []

    def fail(_ref: str) -> str:
        calls.append("call")
        raise error_type("")

    monkeypatch.setattr(lsr, "_resolve_ref_required", fail)
    with pytest.raises(error_type):
        lsr.get_required("FOO_KEY")
    assert calls == ["call"]


@pytest.mark.parametrize(
    "message",
    [
        "401 unauthenticated",
        "403 authorization failed",
        "unauthorized",
        "forbidden",
        "PERMISSION_DENIED",
        "invalid_token",
        "token-invalidated",
        "token is not valid",
        "token revoked",
        "revoked",
        "revoked service account token",
        "503 unavailable: permission denied for invalid token",
        "timeout while authorization failed",
    ],
)
def test_common_auth_forms_classify_auth_and_win_over_transient(message):
    error = RuntimeError(message)
    assert lsr._classify_required_exception(error) is lsr.RequiredSecretAuthError


def test_required_error_never_contains_secret_or_reference(manifest, monkeypatch):
    raw_secret = "opaque-secret-that-must-not-leak"
    raw_ref = "op://vault/item-foo/field"

    def fail(_ref: str) -> str:
        raise RuntimeError(f"503 network failure {raw_secret} {raw_ref}")

    monkeypatch.setattr(lsr, "_resolve_ref_required", fail)
    with pytest.raises(lsr.RequiredSecretFatalError) as excinfo:
        lsr.get_required("FOO_KEY")

    rendered = str(excinfo.value)
    assert raw_secret not in rendered
    assert raw_ref not in rendered
    assert "FOO_KEY" in rendered


def test_concurrent_required_auth_failure_is_single_flight_and_not_retried(
    manifest, monkeypatch
):
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def auth_failure(_ref: str) -> str:
        calls.append("call")
        entered.set()
        release.wait(timeout=2)
        raise lsr.RequiredSecretAuthError("")

    monkeypatch.setattr(lsr, "_resolve_ref_required", auth_failure)
    errors = []

    def resolve() -> None:
        try:
            lsr.get_required("FOO_KEY")
        except lsr.RequiredSecretError as exc:
            errors.append(exc)

    leader = threading.Thread(target=resolve)
    follower = threading.Thread(target=resolve)
    leader.start()
    assert entered.wait(timeout=2)
    follower.start()
    time.sleep(0.05)
    release.set()
    leader.join(timeout=2)
    follower.join(timeout=2)

    assert calls == ["call"]
    assert len(errors) == 2
    assert all(isinstance(exc, lsr.RequiredSecretAuthError) for exc in errors)
