#!/usr/bin/env python3
"""Thin governed Mini entry point for the core read-only incident collector."""
from __future__ import annotations

import sys

from hermes_cli.fleet import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
