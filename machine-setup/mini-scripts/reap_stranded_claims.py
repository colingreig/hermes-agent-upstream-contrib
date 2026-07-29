#!/usr/bin/env python3
"""reap_stranded_claims.py — no_agent cron wrapper for claim_store.py's reaper.

Task 86e261t28 (wave 1): reap stranded ~/.hermes/state/claims/<taskId>.claim
files whose holder is no longer live (age >= TTL, per claim_store.py's own
mtime+TTL liveness policy -- NOT PID-based; executor runs are threads inside
the shared gateway process, so PID can't distinguish holders, see
claim_store.py's module docstring). claim_store.py's own reap_stale() already
implements this correctly; it just had no scheduled invocation anywhere in
jobs.json. This wrapper exists only because cron --script jobs don't support
passing a CLI argument (claim_store.py's own entry point is `claim_store.py
reap`) -- it is a thin, argument-free entry point suitable for --no-agent.

Prints nothing when nothing was reaped (silent run, per the --no-agent
"empty stdout = silent" convention); prints the reaped task ids otherwise.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claim_store import reap_stale  # noqa: E402


def main() -> int:
    reaped = reap_stale()
    if reaped:
        print(f"Reaped {len(reaped)} stranded claim(s): {', '.join(reaped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
