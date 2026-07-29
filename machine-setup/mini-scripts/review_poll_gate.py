#!/usr/bin/env python3
"""Compatibility entrypoint for the packaged Mini review-poll gate."""
from __future__ import annotations

import os

from pr_pipeline.review_poll_gate import main


if __name__ == "__main__":
    if os.environ.get("HERMES_REVIEW_POLL_GATE_IMPORT_SMOKE") == "1":
        if not callable(main):
            raise SystemExit("packaged review-poll main is not callable")
        print("review-poll-gate-import-smoke: ok")
        raise SystemExit(0)
    raise SystemExit(main())
