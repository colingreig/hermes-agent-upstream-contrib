#!/usr/bin/env python3
"""Verify the gateway process env holds only boot-resident vars, never secrets.

Companion check for the per-task 1Password lazy secret resolution migration
(see ``docs/design/per-task-1p-secret-resolution.md``). Dumps the current
process's ``os.environ`` **key names only** — never values — and checks them
against:

- an ALLOWLIST of names expected to remain boot-resident (bucket A —
  session-scoped connections like Slack Socket Mode — and bucket B —
  non-secret config/identifiers).
- a WATCH-LIST of C1 (in-process) + C2 (external-CLI) secret names that
  should have been migrated OFF the boot-export path onto the lazy resolver.
  Any of these still present in ``os.environ`` is a sign of an unmigrated
  or regressed boot export.

This script never prints a secret value — only variable NAMES and
PASS/FAIL. Run it against the live gateway process (e.g. via a debug
endpoint that execs it in-process, or standalone right after boot) to
confirm the migration's acceptance criterion: "no long-lived business
secret in the gateway process env post-boot".

Usage:
    python scripts/verify_gateway_secret_env.py
    python scripts/verify_gateway_secret_env.py --manifest ~/.hermes/scripts/op-secrets.env
"""

from __future__ import annotations

import argparse
import os
import sys

from agent.lazy_secret_resolver import C2_EXTERNAL_CLI_SECRETS as _C2_EXTERNAL_CLI_SECRETS

# ---------------------------------------------------------------------------
# Bucket A — session-scoped, must stay boot-resident (persistent connections).
# ---------------------------------------------------------------------------
_BUCKET_A_SESSION_SCOPED = (
    "SLACK_APP_TOKEN",
    "SLACK_BOT_TOKEN",
)

# ---------------------------------------------------------------------------
# Bucket B — non-secret config / identifiers, fine to stay boot-resident.
# ---------------------------------------------------------------------------
_BUCKET_B_NON_SECRET_CONFIG = (
    "CLICKUP_REVIEW_SLA_DRY_RUN",
    "VALIDATE_SHADOW",
    "HERMES_AUTONOMOUS_MERGE",
    "HERMES_AUTONOMOUS_MERGE_HIGH",
    "HERMES_AUTONOMOUS_MERGE_MEDIUM",
    "HERMES_AUTONOMOUS_MERGE_LOW",
    "HERMES_CONTENT_SONNET",
    "HERMES_WRITER_CODEX",
    "GLM_BASE_URL",
    "SLACK_ALLOWED_USERS",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_EMAIL",
    "GH_APP_ID",
    "GH_APP_INSTALLATION_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "POSTMARK_HERMES_INBOUND_ADDRESS",
    "POSTMARK_HERMES_INBOUND_SERVER_ID",
    "POSTMARK_HERMES_INBOUND_WEBHOOK",
)

ALLOWLIST = frozenset(_BUCKET_A_SESSION_SCOPED) | frozenset(_BUCKET_B_NON_SECRET_CONFIG)

# ---------------------------------------------------------------------------
# C1 — per-task-resolvable secrets consumed in-process. Should be resolved
# lazily via agent.lazy_secret_resolver, never boot-exported.
# ---------------------------------------------------------------------------
_C1_IN_PROCESS_SECRETS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY_HERMES",
    "ZAI_API_KEY",
    "ZAI_API_KEY_HERMES",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MINIMAX_API_KEY",
    "CLICKUP_API_TOKEN",
    "POSTMARK_SERVER_TOKEN",
    "POSTMARK_HERMES_INBOUND_TOKEN",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "MCP_AGENCY_OS_API_KEY",
    "WORKBENCH_MCP_TOKEN",
    "CRON_SECRET",
)

# ---------------------------------------------------------------------------
# C2 — per-task-resolvable secrets consumed by spawned external CLIs
# (vercel/wrangler/git/gh). Should be resolved at spawn time in
# tools/environments/local.py::_make_run_env and injected into the child
# env only, never boot-exported into the gateway parent's os.environ.
# Sourced from agent.lazy_secret_resolver.C2_EXTERNAL_CLI_SECRETS — the
# single source of truth shared with tools/environments/local.py — rather
# than duplicated here.
# ---------------------------------------------------------------------------

WATCHLIST = frozenset(_C1_IN_PROCESS_SECRETS) | frozenset(_C2_EXTERNAL_CLI_SECRETS)


def _load_manifest_names(path: str) -> set[str]:
    """Parse a `KEY=op://vault/item/field` manifest and return the KEY names.

    Never reads values into a variable used for anything but discarding —
    only names are returned. Returns an empty set on any read error
    (fail-open; this is a best-effort cross-check, not a hard dependency).
    """
    names: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, _ref = line.partition("=")
                key = key.strip()
                if key:
                    names.add(key)
    except OSError:
        return set()
    return names


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the gateway process env holds only boot-resident vars. "
            "Prints NAMES and PASS/FAIL only — never values."
        )
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Optional path to an op-secrets.env-style manifest "
            "(KEY=op://vault/item/field lines) to cross-check names against."
        ),
    )
    args = parser.parse_args()

    env_names = set(os.environ.keys())

    leaked = sorted(env_names & WATCHLIST)
    allowlist_present = sorted(env_names & ALLOWLIST)
    allowlist_absent = sorted(ALLOWLIST - env_names)

    print("=== Gateway secret env verification (names only, never values) ===")
    print()

    print(f"Watch-list (C1+C2, {len(WATCHLIST)} names) — should be ABSENT from os.environ:")
    if leaked:
        for name in leaked:
            print(f"  LEAKED (still present): {name}")
    else:
        print("  (none present)")
    print()

    print(f"Allowlist (bucket A+B, {len(ALLOWLIST)} names):")
    print(f"  present ({len(allowlist_present)}):")
    for name in allowlist_present:
        print(f"    {name}")
    print(f"  absent ({len(allowlist_absent)}):")
    for name in allowlist_absent:
        print(f"    {name}")
    print()

    unexpected = sorted(env_names - ALLOWLIST - WATCHLIST)

    if args.manifest:
        manifest_names = _load_manifest_names(args.manifest)
        if manifest_names:
            unknown_in_manifest = sorted(manifest_names - ALLOWLIST - WATCHLIST)
            print(
                f"Manifest cross-check ({args.manifest}, {len(manifest_names)} names):"
            )
            if unknown_in_manifest:
                print("  names in manifest but not in allowlist or watch-list:")
                for name in unknown_in_manifest:
                    print(f"    {name}")
            else:
                print("  all manifest names are classified (allowlist or watch-list)")
            print()
        else:
            print(f"Manifest cross-check: could not read {args.manifest} (skipped)")
            print()

    passed = not leaked

    print(f"Unclassified env vars present (informational, not a fail condition): {len(unexpected)}")
    print()
    print("RESULT: PASS" if passed else "RESULT: FAIL")
    if not passed:
        print(
            f"  {len(leaked)} watch-list (C1/C2) secret name(s) are still present "
            "in os.environ — migration incomplete or regressed."
        )

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
