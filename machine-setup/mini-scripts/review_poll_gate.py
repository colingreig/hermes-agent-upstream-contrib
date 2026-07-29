#!/usr/bin/env python3
"""Compatibility entrypoint for the packaged Mini review-poll gate."""
from __future__ import annotations

from pr_pipeline.review_poll_gate import main


if __name__ == "__main__":
    raise SystemExit(main())
