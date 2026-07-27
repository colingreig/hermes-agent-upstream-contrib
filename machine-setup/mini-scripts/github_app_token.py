#!/usr/bin/env python3
"""Mint one short-lived GitHub App installation token to stdout.

Exit 77 means permanent credential/authentication failure. Exit 75 means a
transient service/network failure. Diagnostics never include credentials or
the minted token.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

import jwt

EXIT_TRANSIENT = 75
EXIT_AUTH = 77


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable {name}")
    return value


def main() -> int:
    try:
        app_id = _required("GH_APP_ID")
        installation_id = _required("GH_APP_INSTALLATION_ID")
        private_key = _required("GH_APP_PRIVATE_KEY").replace("\\n", "\n")
        now = int(time.time())
        signed = jwt.encode(
            {"iat": now - 60, "exp": now + 600, "iss": app_id},
            private_key,
            algorithm="RS256",
        )
        request = urllib.request.Request(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {signed}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hermes-launchd-token-minter",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip():
            print("github_app_token: token missing from successful response", file=sys.stderr)
            return EXIT_TRANSIENT
        print(token.strip())
        return 0
    except ValueError as exc:
        print(f"github_app_token: permanent credential error: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404, 422}:
            print(f"github_app_token: permanent GitHub App error (HTTP {exc.code})", file=sys.stderr)
            return EXIT_AUTH
        print(f"github_app_token: transient GitHub API error (HTTP {exc.code})", file=sys.stderr)
        return EXIT_TRANSIENT
    except (urllib.error.URLError, TimeoutError):
        print("github_app_token: transient GitHub API/network error", file=sys.stderr)
        return EXIT_TRANSIENT
    except Exception as exc:
        print(f"github_app_token: mint failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_AUTH


if __name__ == "__main__":
    raise SystemExit(main())
