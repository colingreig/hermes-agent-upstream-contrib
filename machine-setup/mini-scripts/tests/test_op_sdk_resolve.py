#!/usr/bin/env python3
"""Contract tests for the mini's 1Password SDK resolver.

All network behavior is mocked. These tests must never induce a real 1Password
rate limit.
"""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


DEFAULT_SOURCE = Path(__file__).resolve().parent.parent / "op_sdk_resolve.py"
SOURCE = Path(
    os.environ.get("OP_SDK_RESOLVE_SOURCE", str(DEFAULT_SOURCE))
).expanduser().resolve()

# The SDK is a mini runtime dependency, not a test dependency. Every network
# entry point in this suite is mocked, so provide an import-only stand-in when
# the repository .venv intentionally does not install `onepassword`.
try:
    import onepassword  # noqa: F401
except ModuleNotFoundError:
    onepassword_stub = types.ModuleType("onepassword")
    onepassword_stub.Client = type("Client", (), {})
    sys.modules["onepassword"] = onepassword_stub

spec = importlib.util.spec_from_file_location("op_sdk_resolve_under_test", SOURCE)
resolver = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(resolver)

REF = "op://Test Vault/item/FIELD"


class ResolverContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cache_dir = Path(self.temp_dir.name) / "cache"
        self.cache_patch = mock.patch.object(resolver, "CACHE_DIR", str(self.cache_dir))
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def test_transient_then_success_uses_bounded_jittered_5_15_45_backoff(self):
        calls = 0
        sleeps = []
        jitter_bounds = []

        async def flaky():
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise TimeoutError("service timeout 503")
            return "ok"

        async def fake_sleep(delay):
            sleeps.append(delay)

        def midpoint(low, high):
            jitter_bounds.append((low, high))
            return (low + high) / 2

        with (
            mock.patch.object(resolver.asyncio, "sleep", fake_sleep),
            mock.patch.object(resolver.random, "uniform", midpoint),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            result = asyncio.run(resolver._with_retry(flaky))

        self.assertEqual(result, "ok")
        self.assertEqual(calls, 4)
        self.assertEqual(sleeps, [5.0, 15.0, 45.0])
        self.assertEqual(jitter_bounds, [(4.0, 6.0), (12.0, 18.0), (36.0, 54.0)])
        self.assertIn("retry 3/3 in 45.0s", stderr.getvalue())

    def test_exhausted_transient_without_complete_stale_is_fatal_exit_one(self):
        calls = 0

        async def always_timeout(_refs):
            nonlocal calls
            calls += 1
            raise TimeoutError("service unavailable timeout")

        async def no_sleep(_delay):
            return None

        env_file = Path(self.temp_dir.name) / "secrets.env"
        env_file.write_text(f"KEY={REF}\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(resolver, "_resolve_by_ref", always_timeout),
            mock.patch.object(resolver.asyncio, "sleep", no_sleep),
            mock.patch.object(resolver.random, "uniform", lambda low, high: (low + high) / 2),
            mock.patch.object(sys, "argv", ["op_sdk_resolve.py", str(env_file)]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            rc = resolver.main()

        self.assertEqual(rc, 1)
        self.assertEqual(calls, 4)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("no complete usable stale cache", stderr.getvalue())
        self.assertIn("[op_sdk_resolve] FATAL: authentication/resolution failed:", stderr.getvalue())
        self.assertNotIn("resolved 0/1", stderr.getvalue())

    def test_mixed_auth_and_timeout_markers_fail_immediately(self):
        calls = 0
        sleeps = []

        async def unauthorized():
            nonlocal calls
            calls += 1
            raise RuntimeError("Unauthorized: invalid expired token after timeout 503")

        async def fake_sleep(delay):
            sleeps.append(delay)

        with (
            mock.patch.object(resolver.asyncio, "sleep", fake_sleep),
            redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "Unauthorized"),
        ):
            asyncio.run(resolver._with_retry(unauthorized))

        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])
        self.assertTrue(resolver._is_auth_error(RuntimeError("forbidden auth token")))
        self.assertFalse(
            resolver._is_transient_error(RuntimeError("forbidden auth token timeout 503"))
        )

    def test_exhausted_transient_uses_only_complete_usable_stale_batch(self):
        calls = 0
        refs = {
            "FIRST": "op://Test/item/FIRST",
            "SECOND": "op://Test/item/SECOND",
        }
        for ref in refs.values():
            resolver._write_cache(resolver._cache_key("ref", ref), {"value": f"stale:{ref}"})

        async def always_timeout(_refs):
            nonlocal calls
            calls += 1
            raise TimeoutError("rate limit 429")

        async def no_sleep(_delay):
            return None

        with (
            mock.patch.object(resolver, "_resolve_by_ref", always_timeout),
            mock.patch.object(resolver, "_read_cache_fresh", return_value=None),
            mock.patch.object(resolver.asyncio, "sleep", no_sleep),
            mock.patch.object(resolver.random, "uniform", lambda low, high: (low + high) / 2),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            result = asyncio.run(resolver._resolve_all(refs))

        self.assertEqual(calls, 4)
        self.assertEqual(
            result,
            {key: f"stale:{ref}" for key, ref in refs.items()},
        )
        self.assertIn("serving complete stale cache", stderr.getvalue())

        # An empty value is not usable stale data and must not make a batch
        # look complete.
        self.assertIsNone(resolver._cached_secret_value({"value": ""}))
        self.assertFalse(resolver._usable_fields_payload({"TOKEN": ""}))

    def test_cli_stdout_quoting_contract_is_byte_stable(self):
        env_file = Path(self.temp_dir.name) / "secrets.env"
        env_file.write_text(f"KEY={REF}\n")
        value = 'slash\\ quote" dollar$ tick` newline\nnext'

        async def resolved(_refs):
            return {"KEY": value}

        stdout = io.StringIO()
        with (
            mock.patch.object(resolver, "_resolve_all", resolved),
            mock.patch.object(sys, "argv", ["op_sdk_resolve.py", str(env_file)]),
            redirect_stdout(stdout),
            redirect_stderr(io.StringIO()),
        ):
            rc = resolver.main()

        self.assertEqual(rc, 0)
        self.assertEqual(
            stdout.getvalue().encode(),
            b'KEY="slash\\\\ quote\\" dollar\\$ tick\\` newline\nnext"\n',
        )


if __name__ == "__main__":
    unittest.main()
