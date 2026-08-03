#!/usr/bin/env bash
set -euo pipefail

TEST_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_SENTINEL="$TEST_DIR/../sentinel_run.sh"
SENTINEL="${SENTINEL_RUN_SOURCE:-${1:-$DEFAULT_SENTINEL}}"
bash -n "$SENTINEL"

TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/sentinel-smoke-XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT

mkdir -p \
  "$TMP_ROOT/.config" \
  "$TMP_ROOT/.hermes/scripts" \
  "$TMP_ROOT/.hermes/runtime-current/venv/bin" \
  "$TMP_ROOT/.hermes/logs"
printf 'fake-token\n' > "$TMP_ROOT/.config/op-runtime-token"
printf 'KEY=op://Test/item/FIELD\n' > "$TMP_ROOT/.hermes/scripts/op-secrets.env"
printf '# mocked resolver source\n' > "$TMP_ROOT/.hermes/scripts/op_sdk_resolve.py"

printf '%s\n' \
  '#!/bin/sh' \
  'printf '"'"'KEY="mocked"\n'"'"'' \
  > "$TMP_ROOT/.hermes/runtime-current/venv/bin/python"
chmod +x "$TMP_ROOT/.hermes/runtime-current/venv/bin/python"

HOME="$TMP_ROOT" \
SENTINEL_START_DELAY_MAX_SECONDS=0 \
SENTINEL_SMOKE_ONLY=1 \
bash "$SENTINEL"

grep -q 'smoke-only secrets resolve passed (monitor/Slack suppressed)' \
  "$TMP_ROOT/.hermes/logs/ignite-sentinel.log"

python3 - "$SENTINEL" <<'PY'
from pathlib import Path
import re
import sys

source = Path(sys.argv[1]).read_text()
if not re.search(r"START_DELAY_MAX > 120.*START_DELAY_MAX=120", source, re.DOTALL):
    raise SystemExit("sentinel delay must be clamped to 120 seconds")
if "RANDOM % (START_DELAY_MAX + 1)" not in source:
    raise SystemExit("sentinel delay must cover the inclusive 0-120 second window")
if source.index("sleep \"$START_DELAY\"") > source.index("\"$RESOLVER_PYTHON\" \"$RESOLVER\""):
    raise SystemExit("sentinel must de-cluster before resolving secrets")
PY

echo "sentinel-run-contract: PASS (0-120s de-cluster + Slack-silent smoke)"

# 86e2gda8g: SENTINEL_SMTP_URL fallback export contract. A full (non-smoke)
# run needs a fake ignite-sentinel checkout to `cd`/`exec` into.
SENTINEL_DIR="$TMP_ROOT/.hermes/repos/ignite-sentinel"
mkdir -p "$SENTINEL_DIR/venv/bin"
printf '%s\n' \
  '#!/bin/sh' \
  'env > "$SENTINEL_RUN_TEST_ENV_DUMP"' \
  > "$SENTINEL_DIR/venv/bin/python"
chmod +x "$SENTINEL_DIR/venv/bin/python"
printf 'platforms: {}\n' > "$TMP_ROOT/.hermes/config.yaml"

ENV_DUMP="$TMP_ROOT/full-run-env.txt"
HOME="$TMP_ROOT" \
SENTINEL_START_DELAY_MAX_SECONDS=0 \
POSTMARK_SERVER_TOKEN=test-postmark-token \
SENTINEL_RUN_TEST_ENV_DUMP="$ENV_DUMP" \
bash "$SENTINEL"

grep -q '^SENTINEL_SMTP_URL=smtp://test-postmark-token:test-postmark-token@smtp.postmarkapp.com:587$' \
  "$ENV_DUMP" \
  || { echo "FAIL: SENTINEL_SMTP_URL not constructed from POSTMARK_SERVER_TOKEN" >&2; exit 1; }

echo "sentinel-run-contract: PASS (SENTINEL_SMTP_URL exported when POSTMARK_SERVER_TOKEN present)"

# Absent token: no SENTINEL_SMTP_URL export, and a WARNING is logged instead
# of the run failing closed (Slack stays primary either way).
: > "$TMP_ROOT/.hermes/logs/ignite-sentinel.log"
ENV_DUMP2="$TMP_ROOT/full-run-env-no-token.txt"
HOME="$TMP_ROOT" \
SENTINEL_START_DELAY_MAX_SECONDS=0 \
SENTINEL_RUN_TEST_ENV_DUMP="$ENV_DUMP2" \
bash "$SENTINEL"

if grep -q '^SENTINEL_SMTP_URL=' "$ENV_DUMP2"; then
  echo "FAIL: SENTINEL_SMTP_URL should not be exported without POSTMARK_SERVER_TOKEN" >&2
  exit 1
fi
grep -q 'WARNING: POSTMARK_SERVER_TOKEN not resolved' "$TMP_ROOT/.hermes/logs/ignite-sentinel.log" \
  || { echo "FAIL: missing POSTMARK_SERVER_TOKEN warning log line" >&2; exit 1; }

echo "sentinel-run-contract: PASS (missing POSTMARK_SERVER_TOKEN warns, does not fail closed)"
