#!/usr/bin/env python3
"""Compatibility entrypoint for the packaged validator live trigger."""
from __future__ import annotations

import os

from pr_pipeline.validate_pr_live_trigger import safe_main


if __name__ == "__main__":
    if os.environ.get("HERMES_VALIDATOR_TRIGGER_IMPORT_SMOKE") == "1":
        if not callable(safe_main):
            raise SystemExit("packaged validator live trigger main is not callable")
        print("validator-live-trigger-import-smoke: ok")
        raise SystemExit(0)
    raise SystemExit(safe_main())
