#!/usr/bin/env python3
"""
op_sdk_resolve.py — resolve op:// secret references via the official 1Password
service-account SDK, replacing shell-outs to the `op` CLI.

Why this exists (2026-07-05, live incident): gateway_secrets_wrap.sh's `op read`
probe and `op run --env-file=...` injection were hanging under
OP_SERVICE_ACCOUNT_TOKEN, taking the Hermes gateway down in a continuous
boot-crash loop for hours. Root cause of the CLI hang was never isolated, and a
near-identical hang was already flagged separately on task 86e260vnn. Standing
directive: never shell out to the `op` CLI — use the 1Password service-account
SDK (`onepassword`, importable as `onepassword`) instead, which talks to
1Password's API directly rather than through the CLI/desktop-app integration.

HERMES-PATCH 31 (resilience layer; originally added 2026-07-13 after a 1Password
daily-quota lockout took the gateway down for ~13h; restored 2026-07-21 after it
was lost in the 2026-07-19 home-directory wipe — ClickUp 86e2a99q9/86e2a6p75):
  - A 300s per-key on-disk cache at ~/.cache/op-run/ (0700 dir / 0600 files).
  - Bounded retry/backoff on transient SDK errors (rate-limit/timeout/5xx).
  - Serve-stale-on-error: if a live resolve fails after retries are exhausted,
    fall back to the last cached value (any age) rather than failing closed.
  - A direct items.get(vault_id, item_id) fast path when both refs already
    look like resolved 1Password object ids, skipping vaults.list()/items.list().
  All of this is purely additive and fail-open: any cache read/write error is
  swallowed and resolution proceeds via the original live-SDK path.

CLI usage:
    python_with_sdk op_sdk_resolve.py <env-file-of-KEY=op://ref-lines>
    -> prints resolved KEY=value lines to stdout, one per successfully
       resolved secret. Lines whose reference fails to resolve are skipped
       and reported to stderr (fail-open per-key, not fail-open on auth).

Importable usage:
    from op_sdk_resolve import resolve_refs, resolve_all_fields
    values = resolve_refs(["op://Dev Toolbox/dev/HERMES_CONTENT_SONNET", ...])
    fields = resolve_all_fields("Dev Toolbox", "dev")
    # -> {"HERMES_CONTENT_SONNET": "0", ...}; a field that fails to resolve is
    #    simply absent from the returned dict (fail-open, matching the old
    #    subprocess.run(...) + `except Exception: pass` pattern).

Exit codes (CLI only):
    0  authentication + resolution ran (even if some individual keys failed)
    1  could not authenticate to 1Password at all (token missing/invalid) or
       the SDK itself errored in a way that blocks all resolution
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
from typing import Iterable

from onepassword import Client

TOKEN_FILE = os.path.expanduser("~/.config/op-runtime-token")
INTEGRATION_NAME = "hermes-gateway"
INTEGRATION_VERSION = "v1.0.0"

# --- HERMES-PATCH 31: cache + retry/backoff + serve-stale -------------------
CACHE_DIR = os.path.expanduser("~/.cache/op-run")
CACHE_TTL_SECONDS = 300
_RETRY_DELAYS = (5.0, 15.0, 45.0)
_RETRY_JITTER_FRACTION = 0.20

TRANSIENT_ERROR_MARKERS = (
    "rate limit",
    "rate-limit",
    "too many requests",
    "429",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection refused",
    "connection error",
    "502",
    "503",
    "504",
    "internal server error",
    "internal error",
    "service unavailable",
)

AUTH_ERROR_MARKERS = (
    "auth",
    "unauthorized",
    "invalid",
    "forbidden",
    "expired",
    "token",
)

_OP_ID_RE = re.compile(r"^[a-z0-9]{26}$")


def _looks_like_op_id(value: str) -> bool:
    """True if value looks like a 1Password object id (26 lowercase base32-ish
    chars) rather than a human title — lets callers skip a list+scan."""
    return bool(_OP_ID_RE.match(value))


def _is_auth_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in AUTH_ERROR_MARKERS)


def _is_transient_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    # Authentication failures are permanent until credentials change. Check
    # them first because SDK messages can contain both an auth marker and a
    # transient-looking marker such as "timeout".
    if _is_auth_error(exc):
        return False
    return any(marker in msg for marker in TRANSIENT_ERROR_MARKERS)


async def _with_retry(coro_fn, *args, retry_delays: tuple[float, ...] = _RETRY_DELAYS,
                      jitter_fraction: float = _RETRY_JITTER_FRACTION, **kwargs):
    """Call an async fn with bounded retry/backoff, retrying only errors that
    look transient (rate-limit/timeout/5xx). Non-transient errors (auth
    failure, not-found) raise immediately without burning retries."""
    last_exc: BaseException | None = None
    attempts = len(retry_delays) + 1
    for attempt in range(attempts):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised once retries exhaust
            last_exc = exc
            if not _is_transient_error(exc) or attempt == attempts - 1:
                raise
            nominal_delay = retry_delays[attempt]
            jitter = nominal_delay * max(0.0, jitter_fraction)
            delay = random.uniform(
                max(0.0, nominal_delay - jitter),
                nominal_delay + jitter,
            )
            sys.stderr.write(
                f"[op_sdk_resolve] transient error ({exc!r}), "
                f"retry {attempt + 1}/{len(retry_delays)} in {delay:.1f}s\n"
            )
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable")  # pragma: no cover


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def _ensure_cache_dir() -> None:
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(CACHE_DIR, 0o700)
    except OSError:
        pass


def _write_cache(key: str, payload: dict) -> None:
    """Best-effort cache write; a failure here must never block secret
    resolution, so all errors are swallowed (logged, not raised)."""
    try:
        _ensure_cache_dir()
        path = _cache_path(key)
        tmp_path = f"{path}.tmp-{os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "data": payload}, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError as exc:
        sys.stderr.write(f"[op_sdk_resolve] cache write failed (non-fatal): {exc!r}\n")


def _read_cache_fresh(key: str):
    """Return cached payload only if written within CACHE_TTL_SECONDS, else None."""
    entry = _read_cache_any_age(key)
    if entry is None:
        return None
    ts, data = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        return None
    return data


def _read_cache_any_age(key: str):
    """Serve-stale fallback: return (ts, data) regardless of age, or None if
    no cache entry exists / it's unreadable."""
    try:
        with open(_cache_path(key), encoding="utf-8") as f:
            raw = json.load(f)
        return raw["ts"], raw["data"]
    except (OSError, ValueError, KeyError):
        return None


def _cached_secret_value(data) -> str | None:
    """Return a usable cached secret, rejecting empty/malformed cache data."""
    if not isinstance(data, dict):
        return None
    value = data.get("value")
    if not isinstance(value, str) or not value:
        return None
    return value


def _usable_fields_payload(data) -> bool:
    """A fields cache is safe only when it contains complete usable values."""
    return (
        isinstance(data, dict)
        and bool(data)
        and all(isinstance(value, str) and bool(value) for value in data.values())
    )
# --- end HERMES-PATCH 31 -----------------------------------------------------


def resolve_refs(refs: list[str]) -> dict[str, str]:
    """Sync wrapper: resolve a list of op:// references, return {ref: secret}.

    A ref that fails to resolve (auth error, not found, etc.) is simply
    omitted from the result — fail-open per-ref, same contract as the old
    `subprocess.run(["op", "read", ref]) ... except Exception: pass` call
    sites this replaces. Raises only if authentication itself fails outright
    (missing/invalid token) — callers should catch and fail open the same way
    the old code did around a hung/erroring `op` call.
    """
    unique_refs = list(dict.fromkeys(refs))
    # Route importable callers through the same cache/retry/fail-closed batch
    # path as the CLI. Using each ref as its own key preserves the public
    # {ref: secret} return shape.
    return asyncio.run(_resolve_all({ref: ref for ref in unique_refs}))


async def _resolve_by_ref(unique_refs: list[str]) -> dict[str, str]:
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    client = await Client.authenticate(
        auth=token,
        integration_name=INTEGRATION_NAME,
        integration_version=INTEGRATION_VERSION,
    )
    result = await client.secrets.resolve_all(unique_refs)
    out: dict[str, str] = {}
    for ref, response in result.individual_responses.items():
        content = getattr(response, "content", None)
        if content is not None and hasattr(content, "secret"):
            out[ref] = content.secret
    return out


async def _resolve_all_fields_async(vault_ref: str, item_ref: str) -> dict[str, str]:
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    client = await Client.authenticate(
        auth=token,
        integration_name=INTEGRATION_NAME,
        integration_version=INTEGRATION_VERSION,
    )

    if _looks_like_op_id(vault_ref) and _looks_like_op_id(item_ref):
        # HERMES-PATCH 31 fast path: both refs are already resolved 1Password
        # object ids, so skip vaults.list()/items.list() + the O(n) title scan.
        item = await client.items.get(vault_ref, item_ref)
    else:
        vaults = await client.vaults.list()
        vault = next(
            (
                v
                for v in vaults
                if getattr(v, "id", None) == vault_ref
                or getattr(v, "title", None) == vault_ref
                or getattr(v, "name", None) == vault_ref
            ),
            None,
        )
        if vault is None:
            raise ValueError(f"vault not found: {vault_ref!r}")

        items = await client.items.list(vault.id)
        item_overview = next(
            (
                item
                for item in items
                if getattr(item, "id", None) == item_ref
                or getattr(item, "title", None) == item_ref
                or getattr(item, "name", None) == item_ref
            ),
            None,
        )
        if item_overview is None:
            raise ValueError(f"item not found in vault {vault_ref!r}: {item_ref!r}")

        item = await client.items.get(vault.id, item_overview.id)

    resolved: dict[str, str] = {}
    for field in getattr(item, "fields", []) or []:
        label = getattr(field, "title", None)
        val = getattr(field, "value", None)
        if not label or val is None:
            continue
        if label == "notesPlain":
            continue
        if not (label.replace("_", "").isalnum() and not label[0].isdigit()):
            continue
        resolved[label] = val
    return resolved


def resolve_all_fields(vault_ref: str, item_ref: str) -> dict[str, str]:
    """Resolve every shell-safe field from a 1Password item.

    Mirrors the field filtering rules in ~/.config/op-item-to-env.py so callers
    can replace `op item get --format json | op-item-to-env.py` with the SDK.
    Returns a dict of env var names to raw values.

    HERMES-PATCH 31: served from a 300s cache when fresh; on a live-resolve
    failure (after bounded retry), falls back to the last cached value at any
    age rather than raising — this is the op-run path's serve-stale guard.
    """
    key = _cache_key("fields", vault_ref, item_ref)
    fresh = _read_cache_fresh(key)
    if _usable_fields_payload(fresh):
        return fresh

    try:
        result = asyncio.run(_with_retry(_resolve_all_fields_async, vault_ref, item_ref))
    except Exception as exc:
        stale = _read_cache_any_age(key)
        if stale is not None and _usable_fields_payload(stale[1]):
            _, data = stale
            sys.stderr.write(
                f"[op_sdk_resolve] live resolve failed for {vault_ref}/{item_ref} "
                f"({exc!r}); serving stale cache\n"
            )
            return data
        raise

    _write_cache(key, result)
    return result


def _parse_env_file(path: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, ref = line.partition("=")
            key = key.strip()
            ref = ref.strip()
            if key and ref.startswith("op://"):
                refs[key] = ref
    return refs


async def _resolve_all(refs: dict[str, str]) -> dict[str, str]:
    # De-duplicate refs for the actual API call (some keys intentionally share
    # a ref, e.g. WORKBENCH_MCP_TOKEN / MCP_AGENCY_OS_API_KEY), but resolve
    # per-KEY below so every original key gets emitted, not just one per
    # unique ref.
    #
    # HERMES-PATCH 31: per-ref 300s cache + serve-stale. A ref with a fresh
    # cache entry skips the network call entirely; a ref that fails to
    # resolve live (after retry) falls back to its last cached value at any
    # age instead of being dropped — this is the gateway/sentinel path's
    # serve-stale guard.
    unique_refs = sorted(set(refs.values()))

    secret_by_ref: dict[str, str] = {}
    to_fetch: list[str] = []
    for ref in unique_refs:
        fresh = _read_cache_fresh(_cache_key("ref", ref))
        cached_value = _cached_secret_value(fresh)
        if cached_value is not None:
            secret_by_ref[ref] = cached_value
        else:
            to_fetch.append(ref)

    if to_fetch:
        try:
            live = await _with_retry(_resolve_by_ref, to_fetch)
        except Exception as exc:
            if not _is_transient_error(exc):
                # Non-transient (auth/invalid-token/etc.) failure must not be
                # swallowed into an empty/stale result — propagate so main()'s
                # `except Exception` still produces the FATAL/exit-1 page.
                # Only a transient error that has exhausted _with_retry's
                # bounded attempts may use a complete stale fallback.
                raise
            stale_values: dict[str, str] = {}
            for ref in to_fetch:
                stale = _read_cache_any_age(_cache_key("ref", ref))
                cached_value = _cached_secret_value(stale[1]) if stale is not None else None
                if cached_value is None:
                    sys.stderr.write(
                        f"[op_sdk_resolve] live batch resolve failed ({exc!r}); "
                        f"no complete usable stale cache for ref {ref}\n"
                    )
                    raise
                stale_values[ref] = cached_value
            sys.stderr.write(
                f"[op_sdk_resolve] live batch resolve failed ({exc!r}); "
                f"serving complete stale cache\n"
            )
            for ref, value in stale_values.items():
                secret_by_ref[ref] = value
                sys.stderr.write(
                    f"[op_sdk_resolve] serving stale cache for ref {ref}\n"
                )
        else:
            for ref in to_fetch:
                live_value = live.get(ref)
                if isinstance(live_value, str) and live_value:
                    secret_by_ref[ref] = live_value
                    _write_cache(_cache_key("ref", ref), {"value": live_value})
                else:
                    stale = _read_cache_any_age(_cache_key("ref", ref))
                    cached_value = _cached_secret_value(stale[1]) if stale is not None else None
                    if cached_value is not None:
                        secret_by_ref[ref] = cached_value
                        sys.stderr.write(
                            f"[op_sdk_resolve] serving stale cache for ref {ref}\n"
                        )

    resolved: dict[str, str] = {}
    for key, ref in refs.items():
        if ref in secret_by_ref:
            resolved[key] = secret_by_ref[ref]
        else:
            sys.stderr.write(f"[op_sdk_resolve] FAILED to resolve {key} ({ref})\n")
    return resolved


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: op_sdk_resolve.py <env-file>\n")
        return 1
    env_file = sys.argv[1]
    refs = _parse_env_file(env_file)
    if not refs:
        sys.stderr.write(f"[op_sdk_resolve] no op:// refs found in {env_file}\n")
        return 1

    try:
        resolved = asyncio.run(_resolve_all(refs))
    except Exception as e:
        sys.stderr.write(f"[op_sdk_resolve] FATAL: authentication/resolution failed: {e!r}\n")
        return 1

    sys.stderr.write(
        f"[op_sdk_resolve] resolved {len(resolved)}/{len(refs)} secrets via SDK\n"
    )
    for key, value in resolved.items():
        # Values can contain literal newlines (e.g. GH_APP_PRIVATE_KEY, a PEM
        # key) — keep them embedded as real newlines inside the double quotes
        # (valid bash) rather than encoding to "\n", which `source`/`.` would
        # NOT decode back into an actual newline. Only escape characters that
        # are special inside bash double quotes: backslash, ", $, and `.
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
        )
        print(f'{key}="{escaped}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
