"""Lazy, per-task 1Password secret resolution with an in-memory TTL cache.

Hermes gateway historically exported a subset of 1Password secrets into
``os.environ`` in bulk at process boot (see the mini's
``op_sdk_resolve.py``). That bulk export means every secret is live for the
whole process lifetime and a rotated 1Password item only takes effect after
a restart.

This module is the opposite shape: secrets are resolved **on demand**, one
name at a time, and cached **in-process** (never written to ``os.environ``)
with a TTL so a rotated secret goes live within one TTL window without a
restart.

Fail-open by design. Any error along the way — missing manifest, missing
SDK, a bad/expired service-account token, an unknown name, a timeout, a
network blip — results in ``get()`` returning ``None`` so callers fall back
to whatever lookup they already had (``os.environ``, config.yaml, etc). This
module must never be able to take provider auth down fleet-wide; a hole in
this cache is a "callers use their fallback" event, not an incident.

Concurrency model: ``get()`` may be called from a gateway event-loop thread,
so it must never be able to block for longer than the configured resolve
timeout (default 10s, ``HERMES_LAZY_SECRET_RESOLVE_TIMEOUT``). In steady
state (a warm, unexpired cache entry) ``get()`` never blocks at all — the
cache check is a brief in-memory dict read under a lock. On a cold/expired
ref, the resolving call blocks at most the configured timeout; a hung SDK
call for one ref is resolved on its own throwaway daemon thread and can no
longer wedge resolution of any other ref, or of a subsequent call for the
same ref once the timeout elapses. See ``_resolve_cached`` and
``_resolve_with_timeout`` below for the mechanism.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# Service-account token used to authenticate against the 1Password SDK.
# Mirrors the mini's op_sdk_resolve.py so both paths read the same token.
_TOKEN_PATH = os.environ.get(
    "HERMES_OP_RUNTIME_TOKEN", os.path.expanduser("~/.config/op-runtime-token")
)
_INTEGRATION_NAME = "hermes-gateway"
_INTEGRATION_VERSION = "v1.0.0"

# Manifest of `KEY=op://vault/item/field` lines, one per line.
_DEFAULT_MANIFEST_PATH = os.path.expanduser("~/.hermes/scripts/op-secrets.env")

# Default TTL (seconds) a resolved value stays cached before it is re-resolved.
_DEFAULT_TTL_SECONDS = 600

# Default timeout (seconds) for a single SDK resolution round-trip. Kept
# short because a cold/expired ref can be resolved from a gateway
# event-loop thread — this is the worst-case blocking window.
_DEFAULT_RESOLVE_TIMEOUT_SECONDS = 10

# Secrets consumed by spawned external CLIs (vercel/wrangler/git/gh) rather
# than in-process. Resolved lazily at subprocess-spawn time and injected
# into the CHILD env only (see tools/environments/local.py::_make_run_env),
# never boot-exported into the gateway parent's os.environ. Defined once
# here so it has a single source of truth; both
# tools/environments/local.py and scripts/verify_gateway_secret_env.py
# import this tuple instead of keeping their own copy.
C2_EXTERNAL_CLI_SECRETS = (
    "VERCEL_TOKEN",
    "VERCEL_AUTOMATION_BYPASS_SECRET",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_API_KEY",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GH_APP_PRIVATE_KEY",
)


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
#
# `_lock` guards ONLY the in-memory dicts below (the lazily-parsed manifest
# map, the value cache, and the single-flight registry) — it is NEVER held
# across the SDK I/O call. Each dict access under the lock is a brief,
# bounded operation, so callers resolving *different* refs never contend
# with each other beyond that brief window, and a hung SDK call can't hold
# the lock hostage.
_lock = threading.Lock()

# name -> op:// ref, parsed once from the manifest file.
_name_to_ref: Optional[Dict[str, str]] = None

# ref -> (value, expiry_monotonic)
_cache: Dict[str, Tuple[str, float]] = {}

# ref -> Event, for per-ref single-flight. Only the ref currently being
# resolved (a cold/expired cache miss) has an entry here; the leader
# removes it and sets the Event once resolution finishes (success or
# failure), waking any followers waiting on the same ref.
_inflight: Dict[str, threading.Event] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get(name: str) -> Optional[str]:
    """Resolve one secret by manifest NAME. Returns None on any failure.

    Thread-safe. Returns the cached value if still fresh; otherwise
    resolves it via the 1Password SDK (bounded by
    ``HERMES_LAZY_SECRET_RESOLVE_TIMEOUT``, default 10s) and caches it for
    ``HERMES_LAZY_SECRET_TTL`` seconds (default 600).

    A hung resolution for one name can delay this call up to the resolve
    timeout, but can never block a concurrent ``get()`` for a different
    name, and can never block forever.
    """
    try:
        with _lock:
            ref = _get_name_to_ref_map().get(name)
        if ref is None:
            return None
        return _resolve_cached(ref)
    except Exception:
        logger.warning("lazy_secret_resolver.get failed for name=%r", name, exc_info=True)
        return None


def clear_cache() -> None:
    """Clear the in-memory manifest map and value cache. For tests."""
    global _name_to_ref
    with _lock:
        _name_to_ref = None
        _cache.clear()
        _inflight.clear()


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def _manifest_path() -> str:
    return os.environ.get("HERMES_OP_SECRETS_MANIFEST", _DEFAULT_MANIFEST_PATH)


def _ttl_seconds() -> int:
    try:
        return int(os.environ.get("HERMES_LAZY_SECRET_TTL", _DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS


def _resolve_timeout_seconds() -> float:
    try:
        return float(
            os.environ.get(
                "HERMES_LAZY_SECRET_RESOLVE_TIMEOUT", _DEFAULT_RESOLVE_TIMEOUT_SECONDS
            )
        )
    except (TypeError, ValueError):
        return _DEFAULT_RESOLVE_TIMEOUT_SECONDS


def _get_name_to_ref_map() -> Dict[str, str]:
    """Return the cached name->ref map, parsing the manifest on first use.

    Must be called with ``_lock`` held. A missing/unreadable manifest yields
    an empty map (fail-open) rather than raising.
    """
    global _name_to_ref
    if _name_to_ref is not None:
        return _name_to_ref

    _name_to_ref = _parse_manifest(_manifest_path())
    return _name_to_ref


def _parse_manifest(path: str) -> Dict[str, str]:
    """Parse a `KEY=op://vault/item/field` manifest file into a dict.

    Returns an empty dict on any read/parse error (fail-open) — a missing
    manifest is a normal, expected state (e.g. this profile has no
    lazily-resolved secrets configured), not an error worth raising.
    """
    mapping: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, ref = line.partition("=")
                key = key.strip()
                ref = ref.strip()
                if key and ref:
                    mapping[key] = ref
    except OSError:
        logger.debug("lazy_secret_resolver: manifest unreadable at %s", path)
        return {}
    return mapping


# ---------------------------------------------------------------------------
# Cache + single-flight resolution
# ---------------------------------------------------------------------------


def _resolve_cached(ref: str) -> Optional[str]:
    """Return the cached value for ``ref``, resolving it if needed.

    ``_lock`` guards ONLY the ``_cache``/``_inflight`` dict reads and writes
    below — it is released before any SDK I/O happens.

    The first caller to see a cold/expired entry becomes the "leader": it
    registers a ``threading.Event`` in ``_inflight`` under the lock,
    releases the lock, performs the actual resolution (bounded by the
    configured timeout via ``_resolve_with_timeout``), writes the result to
    ``_cache`` under the lock, pops the ``_inflight`` entry, and sets the
    Event.

    Concurrent callers for the SAME ref ("followers") find the existing
    Event under the lock, release the lock, and wait on it with the same
    timeout instead of firing a redundant SDK call (single-flight, without
    a global bottleneck). A follower that times out returns ``None`` rather
    than blocking indefinitely.

    Callers for DIFFERENT refs never wait on each other's Event and only
    ever contend for the brief dict access under ``_lock`` — so a hung
    resolution for one ref cannot block resolution of any other ref.
    """
    timeout = _resolve_timeout_seconds()

    with _lock:
        cached = _cache.get(ref)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

        event = _inflight.get(ref)
        is_leader = event is None
        if is_leader:
            event = threading.Event()
            _inflight[ref] = event

    if not is_leader:
        if event.wait(timeout=timeout):
            with _lock:
                cached = _cache.get(ref)
                if cached is not None and cached[1] > time.monotonic():
                    return cached[0]
        return None

    value: Optional[str] = None
    try:
        value = _resolve_with_timeout(ref, timeout)
    finally:
        with _lock:
            if value is not None:
                _cache[ref] = (value, time.monotonic() + _ttl_seconds())
            _inflight.pop(ref, None)
            event.set()
    return value


def _resolve_with_timeout(ref: str, timeout: float) -> Optional[str]:
    """Run ``_resolve_ref(ref)`` on a fresh daemon thread, bounded by ``timeout``.

    No shared/persistent executor: every call gets its own brand-new
    ``threading.Thread``, so a hung resolution never exhausts a shared
    worker pool that other refs depend on. The thread delivers its result
    through a ``queue.Queue(maxsize=1)``; the caller waits on the queue
    with ``timeout`` and, if it fires, gives up and returns ``None``. The
    thread is a daemon and is simply abandoned on timeout — it holds no
    lock, so it cannot block any other ``get()`` call, and being a daemon
    it cannot block process shutdown either. Any result it eventually
    produces after the timeout is discarded (the queue is never read
    again).
    """
    result_queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            value = _resolve_ref(ref)
        except Exception:
            value = None
        try:
            result_queue.put_nowait(value)
        except queue.Full:
            pass

    thread = threading.Thread(target=_run, name="op-secret-resolve", daemon=True)
    thread.start()

    try:
        return result_queue.get(timeout=timeout)
    except queue.Empty:
        logger.warning(
            "lazy_secret_resolver: resolution timed out for ref (name withheld); "
            "abandoning resolver thread"
        )
        return None


# ---------------------------------------------------------------------------
# SDK resolution boundary
# ---------------------------------------------------------------------------
#
# `_resolve_ref` is the single seam between this module's cache/manifest
# logic and the actual 1Password SDK call. Tests monkeypatch this function
# directly so the suite never needs the `onepassword` package (which may be
# absent in CI) or network access. It is always invoked through
# `_resolve_with_timeout` (never called directly by cache/single-flight
# logic) so it may block for an arbitrary amount of time without being able
# to wedge anything beyond its own throwaway thread.


def _resolve_ref(ref: str) -> Optional[str]:
    """Resolve a single `op://...` ref via the 1Password SDK. None on failure.

    Fails open on every error path: missing/empty token, missing SDK, or
    any exception raised during resolution. Never logs the resolved value.
    """
    try:
        token = _read_token()
        if not token:
            return None
        return _resolve_ref_in_new_loop(token, ref)
    except Exception:
        logger.warning(
            "lazy_secret_resolver: unexpected error resolving secret", exc_info=True
        )
        return None


def _read_token() -> Optional[str]:
    try:
        with open(_TOKEN_PATH, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


def _resolve_ref_in_new_loop(token: str, ref: str) -> Optional[str]:
    """Run `_resolve_ref_async` on a brand-new event loop in this thread.

    Executed inside the fresh per-call resolver thread spawned by
    ``_resolve_with_timeout``, never on the caller's thread — the caller's
    thread may already be running an event loop, and ``asyncio.run()``
    there would raise ``RuntimeError``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_resolve_ref_async(token, ref))
    finally:
        loop.close()


async def _resolve_ref_async(token: str, ref: str) -> Optional[str]:
    try:
        from onepassword import Client
    except ImportError:
        # The SDK has silently vanished from an environment before and
        # boot-crash-looped the gateway when a hard dependency assumed it
        # was always present. Degrade to None here instead.
        logger.warning("lazy_secret_resolver: onepassword SDK not importable")
        return None

    client = await Client.authenticate(
        auth=token,
        integration_name=_INTEGRATION_NAME,
        integration_version=_INTEGRATION_VERSION,
    )
    results = await client.secrets.resolve_all([ref])
    return _extract_resolved_value(results, ref)


def _extract_resolved_value(results, ref: str) -> Optional[str]:
    """Pull the value for ``ref`` out of a ``resolve_all`` result.

    The SDK's ``resolve_all`` return shape has varied across releases
    (plain dict of ref->value vs. an object with per-ref result entries
    that carry their own error/value). Handle both without assuming a
    single interface, and never raise on an unexpected shape.
    """
    try:
        # Plain-dict shape: {ref: value, ...}
        if isinstance(results, dict):
            entry = results.get(ref)
            return _unwrap_entry(entry)

        # Object shape exposing an "individual_responses" style mapping.
        individual = getattr(results, "individual_responses", None)
        if individual is not None:
            entry = individual.get(ref) if isinstance(individual, dict) else None
            return _unwrap_entry(entry)
    except Exception:
        logger.warning(
            "lazy_secret_resolver: unable to extract resolved value", exc_info=True
        )
    return None


def _unwrap_entry(entry) -> Optional[str]:
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    # Some SDK response shapes wrap the value behind a `.content.secret` or
    # `.value` attribute rather than returning the raw string.
    value = getattr(entry, "value", None)
    if isinstance(value, str):
        return value
    content = getattr(entry, "content", None)
    secret = getattr(content, "secret", None) if content is not None else None
    if isinstance(secret, str):
        return secret
    return None
