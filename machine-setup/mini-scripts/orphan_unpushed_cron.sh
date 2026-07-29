#!/bin/bash
# Read-only orphan/unpushed worktree monitor. The pruner is invoked only in dry-run mode.
set -euo pipefail
exec /Users/colingreig/.hermes/runtime-current/venv/bin/python3.11 /Users/colingreig/.hermes/scripts/prune_orphan_worktrees_86e1zf4r9.py --dry-run --days 30
