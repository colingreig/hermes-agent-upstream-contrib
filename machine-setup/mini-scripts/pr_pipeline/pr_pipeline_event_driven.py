#!/usr/bin/env python3
"""Compatibility shim for the review gate's event-driven validator wake."""
from __future__ import annotations

if __package__:
    from .pr_pipeline_improvements import wake_validator_if_needed
else:
    from pr_pipeline_improvements import wake_validator_if_needed

__all__ = ["wake_validator_if_needed"]
