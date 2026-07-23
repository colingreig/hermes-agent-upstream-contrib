#!/usr/bin/env bash
set -euo pipefail

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MINI_SCRIPTS_DIR=$(cd "$TEST_DIR/.." && pwd)
REPO_ROOT=$(cd "$TEST_DIR/../../.." && pwd)

SOURCE="${OP_SDK_RESOLVE_SOURCE:-$MINI_SCRIPTS_DIR/op_sdk_resolve.py}"
LIVE="${OP_SDK_RESOLVE_LIVE_SOURCE:-$SOURCE}"
SENTINEL="${SENTINEL_RUN_SOURCE:-$MINI_SCRIPTS_DIR/sentinel_run.sh}"
DEGRADED="${DEGRADED_SECRETS_MONITOR_SOURCE:-$MINI_SCRIPTS_DIR/degraded_secrets_monitor.py}"
MARKETPLACE="${IGNITE_MARKETPLACE_SYNC_SOURCE:-$MINI_SCRIPTS_DIR/ignite-marketplace-sync.sh}"
PYTHON="${OP_SDK_RESOLVE_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON=python3
fi

cmp -s "$SOURCE" "$LIVE"
bash -n "$SENTINEL"
"$PYTHON" - "$SOURCE" "$DEGRADED" <<'PY'
from pathlib import Path
import sys

for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    compile(path.read_text(), str(path), "exec")
PY

grep -Fq \
  'RESOLVER_PYTHON = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python")' \
  "$DEGRADED"
grep -Fq 'SENTINEL_SMOKE_ONLY' "$SENTINEL"

marketplace_result="marketplace=not-bundled"
if [ -f "$MARKETPLACE" ]; then
  bash -n "$MARKETPLACE"
  grep -Fq 'from op_sdk_resolve import resolve_refs' "$MARKETPLACE"
  marketplace_result="marketplace=verified"
fi

echo "op-sdk-consumers: PASS (source identity + sentinel/degraded; $marketplace_result)"
