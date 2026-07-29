#!/usr/bin/env bash
# Install the source-controlled, read-only MacBook delivery watcher LaunchAgent.
set -euo pipefail

ENABLE=0
case "${1:-}" in
  --install) ;;
  --install-and-enable) ENABLE=1 ;;
  -h|--help|"")
    echo "Usage: install-hermes-delivery-watcher.sh --install|--install-and-enable"
    exit 0
    ;;
  *)
    echo "ERROR: unknown argument: ${1:-}" >&2
    exit 2
    ;;
esac

[ "$(uname -s)" = "Darwin" ] || {
  echo "ERROR: the delivery watcher LaunchAgent is macOS-only" >&2
  exit 1
}

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
WATCHER_SOURCE="$SCRIPT_DIR/hermes_delivery_watch.py"
CORRELATOR_SOURCE="$SCRIPT_DIR/task_delivery.py"
PRODUCER_SOURCE="$SCRIPT_DIR/hermes_delivery_snapshot.py"
SOURCE_PLIST="$SCRIPT_DIR/launchd/com.colingreig.hermes.delivery-watch.plist"
TARGET_DIR="$HERMES_HOME/libexec/delivery-watch"
WATCHER="$TARGET_DIR/hermes_delivery_watch.py"
CORRELATOR="$TARGET_DIR/task_delivery.py"
PRODUCER="$TARGET_DIR/hermes_delivery_snapshot.py"
WATCH_CONFIG="$HERMES_HOME/config.delivery-watch.yaml"
SNAPSHOT="$HERMES_HOME/state/delivery-input/macbook.json"
TARGET_PLIST="$HOME/Library/LaunchAgents/com.colingreig.hermes.delivery-watch.plist"
LABEL="com.colingreig.hermes.delivery-watch"
DOMAIN="gui/$(id -u)"

[ -f "$WATCHER_SOURCE" ] && [ ! -L "$WATCHER_SOURCE" ] \
  || { echo "ERROR: missing or symlinked watcher source: $WATCHER_SOURCE" >&2; exit 1; }
[ -f "$CORRELATOR_SOURCE" ] && [ ! -L "$CORRELATOR_SOURCE" ] \
  || { echo "ERROR: missing or symlinked correlator source: $CORRELATOR_SOURCE" >&2; exit 1; }
[ -f "$PRODUCER_SOURCE" ] && [ ! -L "$PRODUCER_SOURCE" ] \
  || { echo "ERROR: missing or symlinked producer source: $PRODUCER_SOURCE" >&2; exit 1; }
[ -f "$SOURCE_PLIST" ] && [ ! -L "$SOURCE_PLIST" ] \
  || { echo "ERROR: missing or symlinked source plist: $SOURCE_PLIST" >&2; exit 1; }

PYTHON=""
python_candidates=()
[ -n "${HERMES_DELIVERY_WATCH_PYTHON:-}" ] \
  && python_candidates+=("$HERMES_DELIVERY_WATCH_PYTHON")
python_candidates+=(
  "$REPO_ROOT/.venv/bin/python"
  "$REPO_ROOT/venv/bin/python"
)
command -v python3 >/dev/null 2>&1 \
  && python_candidates+=("$(command -v python3)")
python_candidates+=("/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3")
for candidate in "${python_candidates[@]}"; do
  [ -x "$candidate" ] || continue
  if PYTHONPATH="$SCRIPT_DIR" "$candidate" -c \
    'import yaml, task_delivery, hermes_delivery_watch, hermes_delivery_snapshot' >/dev/null 2>&1; then
    PYTHON="$(CDPATH= cd -- "$(dirname -- "$candidate")" && pwd -P)/$(basename "$candidate")"
    break
  fi
done
[ -n "$PYTHON" ] || {
  echo "ERROR: no Python can import the watcher and required PyYAML dependency" >&2
  exit 1
}

mkdir -p "$HERMES_HOME/logs" "$HERMES_HOME/state/task-delivery-watch" \
  "$HERMES_HOME/state/delivery-input" "$TARGET_DIR"
chmod 0700 "$TARGET_DIR"

install_source() {
  local source="$1"
  local destination="$2"
  local mode="$3"
  local swap
  swap="$(mktemp "$TARGET_DIR/.${destination##*/}.swap.XXXXXX")"
  cp "$source" "$swap"
  chmod "$mode" "$swap"
  mv -f "$swap" "$destination"
}
install_source "$WATCHER_SOURCE" "$WATCHER" 0755
install_source "$CORRELATOR_SOURCE" "$CORRELATOR" 0644
install_source "$PRODUCER_SOURCE" "$PRODUCER" 0755

# Prove the exact installed files load under the interpreter written into the
# LaunchAgent.
PYTHONPATH="$TARGET_DIR" "$PYTHON" -c \
  'import yaml, task_delivery, hermes_delivery_watch, hermes_delivery_snapshot' >/dev/null
"$PYTHON" "$WATCHER" --status >/dev/null

# Use a dedicated watcher config so installing the observer never rewrites the
# user's main Hermes behavioral config.  Existing configuration is preserved
# byte-for-byte for review and optional Slack/dead-man additions.
if [ ! -e "$WATCH_CONFIG" ]; then
  "$PYTHON" - "$WATCH_CONFIG" "$SNAPSHOT" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

target = Path(sys.argv[1])
snapshot = str(Path(sys.argv[2]))
payload = {
    "delivery_snapshot": {
        "clickup_list_id": "901714465284",
        "lookback_hours": 72,
        "max_tasks": 40,
        "mini_host": "mini",
    },
    "delivery_watch": {
        "collectors": [
            {
                "kind": "file",
                "name": "live-delivery-snapshot",
                "path": snapshot,
            }
        ]
    },
}
target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PY
  echo "Created reviewable watcher config $WATCH_CONFIG"
else
  [ -f "$WATCH_CONFIG" ] && [ ! -L "$WATCH_CONFIG" ] \
    || { echo "ERROR: watcher config must be a regular non-symlink: $WATCH_CONFIG" >&2; exit 1; }
  echo "Preserved existing watcher config $WATCH_CONFIG"
fi
chmod 0600 "$WATCH_CONFIG"
PYTHONPATH="$TARGET_DIR" "$PYTHON" - "$WATCH_CONFIG" <<'PY'
import sys
from pathlib import Path
from hermes_delivery_snapshot import _load_config as load_snapshot_config
from hermes_delivery_watch import _load_config as load_watch_config

path = Path(sys.argv[1])
load_snapshot_config(path)
load_watch_config(path)
PY

mkdir -p "$(dirname "$TARGET_PLIST")"
tmp="$(mktemp "$(dirname "$TARGET_PLIST")/.${LABEL}.swap.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
"$PYTHON" - "$SOURCE_PLIST" "$tmp" "$PYTHON" "$PRODUCER" "$WATCH_CONFIG" \
  "$SNAPSHOT" "$HOME" "$HERMES_HOME" <<'PY'
import sys
from pathlib import Path

source, target, python, producer, config, snapshot, home, hermes_home = sys.argv[1:]
payload = Path(source).read_text(encoding="utf-8")
for marker, value in (
    ("__HERMES_DELIVERY_WATCH_PYTHON__", python),
    ("__HERMES_DELIVERY_SNAPSHOT_SCRIPT__", producer),
    ("__HERMES_DELIVERY_WATCH_CONFIG__", config),
    ("__HERMES_DELIVERY_SNAPSHOT_OUTPUT__", snapshot),
    ("__HOME__", home),
    ("__HERMES_HOME__", hermes_home),
):
    payload = payload.replace(marker, value)
Path(target).write_text(payload, encoding="utf-8")
PY
chmod 0644 "$tmp"
plutil -lint "$tmp" >/dev/null
mv -fh "$tmp" "$TARGET_PLIST"
trap - EXIT
echo "Installed $TARGET_PLIST"

if [ "$ENABLE" -eq 1 ]; then
  PYTHONPATH="$TARGET_DIR" "$PYTHON" - "$WATCH_CONFIG" <<'PY'
import sys
from pathlib import Path
from hermes_delivery_snapshot import _load_config as load_snapshot_config
from hermes_delivery_watch import _load_config as load_watch_config

path = Path(sys.argv[1])
load_snapshot_config(path)
load_watch_config(path)
PY
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
  echo "Enabled $DOMAIN/$LABEL"
else
  echo "Not loaded. Review $WATCH_CONFIG, then re-run with --install-and-enable."
fi
