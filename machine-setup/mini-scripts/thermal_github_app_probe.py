#!/usr/bin/env python3
"""Body-suppressed GitHub App probes for thermal runner registration.

Mints an installation token via the governed github_app_token.py minter (or
accepts GITHUB_TOKEN for hermetic tests), then exercises the thermal repo's
runners list and registration-token endpoints. Response bodies are never printed,
logged, or persisted — only HTTP status codes are emitted.

Usage (Mini, governed):
  op-run --env-file ~/.hermes/github-app.op-env -- \\
    ~/.hermes/runtime-current/venv/bin/python \\
    ~/.hermes/scripts/thermal_github_app_probe.py

Exit 0 when both probes return the expected status codes (GET 200, POST 201).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_OWNER = "colingreig"
DEFAULT_REPO = "thermal"
EXPECTED_GET = 200
EXPECTED_POST = 201
EXIT_PROBE_FAIL = 1
EXIT_MINT_FAIL = 77


def _mint_token() -> str:
    env_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_token:
        return env_token

    minter = os.environ.get(
        "HERMES_GITHUB_APP_TOKEN_MINTER",
        str(Path.home() / ".hermes/scripts/github_app_token.py"),
    )
    python = os.environ.get(
        "HERMES_PYTHON",
        str(Path.home() / ".hermes/runtime-current/venv/bin/python"),
    )
    proc = subprocess.run(
        [python, minter],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(
            f"thermal_github_app_probe: token mint failed (exit {proc.returncode})",
            file=sys.stderr,
        )
        if detail:
            print(detail[:300], file=sys.stderr)
        raise SystemExit(EXIT_MINT_FAIL)
    token = proc.stdout.strip()
    if not token:
        print("thermal_github_app_probe: empty token from minter", file=sys.stderr)
        raise SystemExit(EXIT_MINT_FAIL)
    return token


def _probe(method: str, url: str, token: str) -> int:
    request = urllib.request.Request(
        url,
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-thermal-github-app-probe",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            # Drain body without printing or storing it.
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code


def main() -> int:
    owner = os.environ.get("THERMAL_PROBE_OWNER", DEFAULT_OWNER)
    repo = os.environ.get("THERMAL_PROBE_REPO", DEFAULT_REPO)
    base = f"https://api.github.com/repos/{owner}/{repo}/actions/runners"

    token = _mint_token()
    get_code = _probe("GET", base, token)
    post_code = _probe("POST", f"{base}/registration-token", token)
    del token

    print(json.dumps({"GET_runners": get_code, "POST_registration_token": post_code}))

    if get_code != EXPECTED_GET or post_code != EXPECTED_POST:
        print(
            f"thermal_github_app_probe: unexpected status "
            f"(expected GET {EXPECTED_GET}, POST {EXPECTED_POST})",
            file=sys.stderr,
        )
        return EXIT_PROBE_FAIL
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
