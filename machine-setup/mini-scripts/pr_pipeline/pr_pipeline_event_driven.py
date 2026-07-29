#!/usr/bin/env python3
"""Review gate event-driven validator wake."""
from __future__ import annotations

from .pr_pipeline_improvements import wake_validator_if_needed

__all__ = ["wake_validator_if_needed"]
