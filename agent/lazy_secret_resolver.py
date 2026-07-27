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
import re
import threading
import time
import weakref
from typing import Dict, Optional, Tuple, Type

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

# Strict callers are used only for explicitly declared, required child
# credentials.  A transient 1Password failure gets two bounded retries; auth,
# missing, and fatal failures do not become retry loops.
_REQUIRED_ATTEMPTS = 3
_REQUIRED_RETRY_DELAYS = (0.2, 0.5)

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

# name -> op:// ref, refreshed when the manifest's stat identity changes.
_name_to_ref: Optional[Dict[str, str]] = None
_manifest_identity: Optional[Tuple[str, Optional[int], Optional[int], Optional[int], Optional[int]]] = None

# ref -> (value, expiry_monotonic)
_cache: Dict[str, Tuple[str, float]] = {}

# ref -> Event, for per-ref single-flight. Only the ref currently being
# resolved (a cold/expired cache miss) has an entry here; the leader
# removes it and sets the Event once resolution finishes (success or
# failure), waking any followers waiting on the same ref.
_inflight: Dict[str, threading.Event] = {}
_inflight_errors: "weakref.WeakKeyDictionary[threading.Event, RequiredSecretError]"


class RequiredSecretError(RuntimeError):
    """Base class for sanitized strict-resolution failures."""

    classification = "fatal"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"required secret {name!r} is unavailable "
            f"(classification={self.classification})"
        )


class RequiredSecretMissingError(RequiredSecretError):
    classification = "missing"


class RequiredSecretAuthError(RequiredSecretError):
    classification = "auth"


class RequiredSecretTransientError(RequiredSecretError):
    classification = "transient"


class RequiredSecretFatalError(RequiredSecretError):
    classification = "fatal"


_inflight_errors = weakref.WeakKeyDictionary()


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


def get_required(name: str) -> str:
    """Resolve a declared required secret or raise a typed, sanitized error.

    Unlike :func:`get`, this API is intentionally fail-closed.  It is for
    callers that have explicitly declared that a child process cannot run
    safely without the value.  Only transient failures are retried, for a
    fixed maximum of three attempts with short bounded delays.
    """
    try:
        with _lock:
            ref = _get_name_to_ref_map().get(name)
        if ref is None:
            raise RequiredSecretMissingError(name)

        for attempt in range(_REQUIRED_ATTEMPTS):
            try:
                return _resolve_required_cached(ref)
            except RequiredSecretTransientError:
                if attempt >= _REQUIRED_ATTEMPTS - 1:
                    raise RequiredSecretTransientError(name) from None
                time.sleep(_REQUIRED_RETRY_DELAYS[attempt])
            except RequiredSecretError as exc:
                raise type(exc)(name) from None
    except RequiredSecretError:
        raise
    except Exception:
        raise RequiredSecretFatalError(name) from None

    # The loop either returns or raises. Keep a defensive terminal branch for
    # type checkers and future edits.
    raise RequiredSecretFatalError(name)


def clear_cache() -> None:
    """Clear the in-memory manifest map and value cache. For tests."""
    global _manifest_identity, _name_to_ref
    with _lock:
        _name_to_ref = None
        _manifest_identity = None
        _cache.clear()
        _inflight.clear()
        _inflight_errors.clear()


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
    """Return the name->ref map, refreshing when the manifest identity changes.

    The identity includes path, device, inode, size, and nanosecond mtime.
    Rotation by atomic replace is therefore visible immediately, even when
    the replacement happens inside a value-cache TTL. Must be called with
    ``_lock`` held. A missing/unreadable manifest yields an empty map
    (fail-open) rather than raising.
    """
    global _manifest_identity, _name_to_ref
    path = _manifest_path()
    identity = _manifest_stat_identity(path)
    if _name_to_ref is not None and identity == _manifest_identity:
        return _name_to_ref

    _name_to_ref = _parse_manifest(path)
    _manifest_identity = identity
    return _name_to_ref


def _manifest_stat_identity(
    path: str,
) -> Tuple[str, Optional[int], Optional[int], Optional[int], Optional[int]]:
    try:
        stat_result = os.stat(path)
    except OSError:
        return (os.path.abspath(path), None, None, None, None)
    return (
        os.path.abspath(path),
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


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


def _resolve_required_cached(ref: str) -> str:
    """Strict counterpart to ``_resolve_cached`` with the same cache/flight."""
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
                prior_error = _inflight_errors.get(event)
            if prior_error is not None:
                raise type(prior_error)("")
        raise RequiredSecretTransientError("")

    value: Optional[str] = None
    error: Optional[RequiredSecretError] = None
    try:
        value = _resolve_required_with_timeout(ref, timeout)
    except RequiredSecretError as exc:
        error = exc
    finally:
        with _lock:
            if value is not None:
                _cache[ref] = (value, time.monotonic() + _ttl_seconds())
            elif error is not None:
                _inflight_errors[event] = error
            _inflight.pop(ref, None)
            event.set()

    if error is not None:
        raise error
    if value is None:
        raise RequiredSecretMissingError("")
    return value


def _resolve_required_with_timeout(ref: str, timeout: float) -> str:
    result_queue: "queue.Queue[Tuple[str, object]]" = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            result: Tuple[str, object] = ("value", _resolve_ref_required(ref))
        except RequiredSecretError as exc:
            result = ("error", exc)
        except Exception:
            result = ("error", RequiredSecretFatalError(""))
        try:
            result_queue.put_nowait(result)
        except queue.Full:
            pass

    thread = threading.Thread(
        target=_run,
        name="op-secret-resolve-required",
        daemon=True,
    )
    thread.start()

    try:
        kind, payload = result_queue.get(timeout=timeout)
    except queue.Empty:
        raise RequiredSecretTransientError("") from None
    if kind == "error":
        if isinstance(payload, RequiredSecretError):
            raise payload
        raise RequiredSecretFatalError("")
    if not isinstance(payload, str) or not payload:
        raise RequiredSecretMissingError("")
    return payload


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


def _resolve_ref_required(ref: str) -> str:
    token = _read_token()
    if not token:
        raise RequiredSecretAuthError("")
    return _resolve_ref_in_new_loop_required(token, ref)


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


def _resolve_ref_in_new_loop_required(token: str, ref: str) -> str:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_resolve_ref_async_required(token, ref))
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


async def _resolve_ref_async_required(token: str, ref: str) -> str:
    try:
        from onepassword import Client
    except ImportError:
        raise RequiredSecretFatalError("") from None

    try:
        client = await Client.authenticate(
            auth=token,
            integration_name=_INTEGRATION_NAME,
            integration_version=_INTEGRATION_VERSION,
        )
        results = await client.secrets.resolve_all([ref])
    except Exception as exc:
        error_type = _classify_required_exception(exc)
        raise error_type("") from None

    value = _extract_resolved_value(results, ref)
    if isinstance(value, str) and value:
        return value

    entry_error = _extract_entry_error(results, ref)
    if entry_error is not None:
        error_type = _classify_required_exception(entry_error)
        # A per-item unknown/not-found response is a missing declaration, not
        # an infrastructure retry. Auth still wins when both signals appear.
        if error_type is RequiredSecretFatalError and _looks_missing(entry_error):
            error_type = RequiredSecretMissingError
        raise error_type("") from None
    raise RequiredSecretMissingError("")


def _exception_fingerprint(exc: object) -> str:
    """Return classification-only metadata; never returned or logged."""
    parts = [type(exc).__name__]
    for attr in ("status", "status_code", "code"):
        try:
            value = getattr(exc, attr, None)
        except Exception:
            value = None
        if value is not None:
            parts.append(str(value))
    try:
        parts.append(str(exc))
    except Exception:
        pass
    return " ".join(parts).lower()


def _classify_required_exception(
    exc: object,
) -> Type[RequiredSecretError]:
    fingerprint = _exception_fingerprint(exc)
    normalized = re.sub(r"[_-]+", " ", fingerprint)

    # Authentication/authorization is checked first so a mixed server message
    # cannot be misclassified as retryable.
    auth_markers = (
        "authentication",
        "authorization",
        "unauthenticated",
        "unauthorized",
        "forbidden",
        "invalid token",
        "token invalid",
        "token invalidated",
        "token revoked",
        "revoked",
        "token is not valid",
        "expired token",
        "token expired",
        "permission denied",
    )
    if (
        re.search(r"\b(401|403)\b", normalized)
        or re.search(
            r"\b(?:auth|authentication|authorization|unauthenticated|"
            r"unauthorized|forbidden)\b",
            normalized,
        )
        or re.search(
            r"\b(?:invalid|expired|revoked)\b.{0,48}\btoken\b"
            r"|\btoken\b.{0,48}\b(?:invalid|invalidated|expired|revoked)\b"
            r"|\btoken\b.{0,24}\bnot\s+valid\b",
            normalized,
        )
        or any(marker in normalized for marker in auth_markers)
    ):
        return RequiredSecretAuthError

    transient_markers = (
        "408",
        "409",
        "425",
        "429",
        "500",
        "502",
        "503",
        "504",
        "timeout",
        "timed out",
        "temporar",
        "unavailable",
        "rate limit",
        "connection",
        "network",
        "transport",
    )
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or any(
        marker in normalized for marker in transient_markers
    ):
        return RequiredSecretTransientError
    return RequiredSecretFatalError


def _looks_missing(exc: object) -> bool:
    fingerprint = _exception_fingerprint(exc)
    return any(
        marker in fingerprint
        for marker in ("404", "not found", "unknown item", "missing secret")
    )


def _extract_entry_error(results: object, ref: str) -> Optional[object]:
    try:
        if isinstance(results, dict):
            entry = results.get(ref)
        else:
            individual = getattr(results, "individual_responses", None)
            entry = individual.get(ref) if isinstance(individual, dict) else None
        if entry is None:
            return None
        return getattr(entry, "error", None)
    except Exception:
        return None


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
