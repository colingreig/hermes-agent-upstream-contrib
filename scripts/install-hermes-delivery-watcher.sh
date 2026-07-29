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
WATCHER_SOURCE="$SCRIPT_DIR/hermes_delivery_watch.py"
CORRELATOR_SOURCE="$SCRIPT_DIR/task_delivery.py"
PRODUCER_SOURCE="$SCRIPT_DIR/hermes_delivery_snapshot.py"
SAFETY_SOURCE="$SCRIPT_DIR/delivery_watch_safety.py"
SOURCE_PLIST="$SCRIPT_DIR/launchd/com.colingreig.hermes.delivery-watch.plist"
TARGET_DIR="$HERMES_HOME/libexec/delivery-watch"
WATCHER="$TARGET_DIR/hermes_delivery_watch.py"
CORRELATOR="$TARGET_DIR/task_delivery.py"
PRODUCER="$TARGET_DIR/hermes_delivery_snapshot.py"
SAFETY="$TARGET_DIR/delivery_watch_safety.py"
WATCH_CONFIG="$HERMES_HOME/config.delivery-watch.json"
LEGACY_WATCH_CONFIG="$HERMES_HOME/config.delivery-watch.yaml"
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
[ -f "$SAFETY_SOURCE" ] && [ ! -L "$SAFETY_SOURCE" ] \
  || { echo "ERROR: missing or symlinked safety source: $SAFETY_SOURCE" >&2; exit 1; }
[ -f "$SOURCE_PLIST" ] && [ ! -L "$SOURCE_PLIST" ] \
  || { echo "ERROR: missing or symlinked source plist: $SOURCE_PLIST" >&2; exit 1; }

PYTHON="/usr/bin/python3"
if [ -n "${HERMES_DELIVERY_WATCH_PYTHON:-}" ] \
  && [ "$HERMES_DELIVERY_WATCH_PYTHON" != "$PYTHON" ]; then
  echo "ERROR: watcher interpreter override is not trusted; required path is $PYTHON" >&2
  exit 1
fi
[ -x "$PYTHON" ] && [ ! -L "$PYTHON" ] || {
  echo "ERROR: required Apple Python is missing or symlinked: $PYTHON" >&2
  exit 1
}
/usr/bin/codesign --verify --strict -R='anchor apple' "$PYTHON" >/dev/null 2>&1 || {
  echo "ERROR: required Python does not satisfy the Apple code-signing requirement" >&2
  exit 1
}
"$PYTHON" -c \
  'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info < (4, 0) else 1)' \
  || {
    echo "ERROR: $PYTHON must be supported Python 3.9 or newer" >&2
    exit 1
  }
PYTHONPATH="$SCRIPT_DIR" "$PYTHON" -c \
  'import task_delivery, hermes_delivery_watch, hermes_delivery_snapshot' >/dev/null \
  || {
    echo "ERROR: watcher sources do not load under the trusted Apple Python" >&2
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
install_source "$SAFETY_SOURCE" "$SAFETY" 0644

# Prove the exact installed files load under the interpreter written into the
# LaunchAgent.
PYTHONPATH="$TARGET_DIR" "$PYTHON" -c \
  'import task_delivery, hermes_delivery_watch, hermes_delivery_snapshot' >/dev/null
"$PYTHON" "$WATCHER" --status >/dev/null

# Use a dedicated watcher config so installing the observer never rewrites the
# user's main Hermes behavioral config. Existing JSON configuration is
# preserved byte-for-byte. The installer's former JSON-in-YAML file is migrated
# once without modifying or deleting the legacy copy.
if [ ! -e "$WATCH_CONFIG" ]; then
  if [ -e "$LEGACY_WATCH_CONFIG" ]; then
    [ -f "$LEGACY_WATCH_CONFIG" ] && [ ! -L "$LEGACY_WATCH_CONFIG" ] \
      || { echo "ERROR: legacy watcher config must be a regular non-symlink: $LEGACY_WATCH_CONFIG" >&2; exit 1; }
    "$PYTHON" - "$LEGACY_WATCH_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"ERROR: legacy watcher config must contain valid JSON: {exc}")
if not isinstance(value, dict):
    raise SystemExit("ERROR: legacy watcher config must contain a JSON object")
PY
    swap="$(mktemp "$HERMES_HOME/.config.delivery-watch.json.swap.XXXXXX")"
    cp "$LEGACY_WATCH_CONFIG" "$swap"
    chmod 0600 "$swap"
    mv -f "$swap" "$WATCH_CONFIG"
    echo "Migrated legacy JSON watcher config to $WATCH_CONFIG; preserved $LEGACY_WATCH_CONFIG"
  else
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
        "max_tasks": 25,
        "mini_host": "mini",
        "poll_timeout_seconds": 240,
        "governing_ci": [
            {
                "path": ".github/workflows/ci.yml",
                "events": ["pull_request", "push"],
            }
        ],
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
  fi
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
"$PYTHON" - "$SOURCE_PLIST" "$tmp" "$PRODUCER" "$WATCH_CONFIG" \
  "$SNAPSHOT" "$HOME" "$HERMES_HOME" <<'PY'
import sys
from pathlib import Path

source, target, producer, config, snapshot, home, hermes_home = sys.argv[1:]
payload = Path(source).read_text(encoding="utf-8")
for marker, value in (
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
