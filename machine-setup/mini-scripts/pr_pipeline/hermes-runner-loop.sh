#!/usr/bin/env bash
# Serve one ephemeral GitHub Actions job, preserve bounded private diagnostics,
# then remove the per-job work tree before systemd starts the next cycle.
set -uo pipefail
umask 077

REPO="${1:?usage: hermes-runner-loop.sh <repo>}"
if [ "${HERMES_RUNNER_TEST_MODE:-0}" = "1" ]; then
  BASE="${HERMES_RUNNER_TEST_HOME:?test home required}"
  HERMES_CI="${HERMES_RUNNER_TEST_ROOT:?test root required}"
else
  BASE="/home/colingreig"
  HERMES_CI="$BASE/.hermes-ci"
fi
DIR="$BASE/actions-runner/$REPO"
# Governed repos were transferred colingreig -> ignitemarketing (2026-08-06);
# the registration-token mint and config.sh --url below used to hardcode
# colingreig and 404 for every transferred repo (migration-learnings #18).
APP_PEM="$HERMES_CI/executor-app.pem"
CONFIG="$HERMES_CI/runner-config.json"
REGISTRATION="$HERMES_CI/runner-registration.json"
SLOTS_DIR="$HERMES_CI/sem/slots"
ARCHIVE_ROOT="$HERMES_CI/diagnostics/$REPO"
FAILURE_ARCHIVE_ROOT="$HERMES_CI/diagnostics-failures/$REPO"
RUNTIME_DIR="$HERMES_CI/runtime"
NAME="hermes-$REPO"
MARK="$SLOTS_DIR/$NAME"

has_job_evidence() {
  [ -f "$RUNTIME_DIR/$NAME.env" ] || return 1
  grep -Eq '^run_id=[^[:space:]]+$' "$RUNTIME_DIR/$NAME.env" \
    && ! grep -Eq '^run_id=(unknown)?$' "$RUNTIME_DIR/$NAME.env" \
    && grep -Eq '^job=[^[:space:]]+$' "$RUNTIME_DIR/$NAME.env" \
    && ! grep -Eq '^job=(unknown)?$' "$RUNTIME_DIR/$NAME.env"
}

has_diag_evidence() {
  [ -d "$DIR/_diag" ] && find "$DIR/_diag" -type f -print -quit | grep -q .
}

# Owner resolution is deliberately fail-closed: an explicit HERMES_RUNNER_OWNER
# override always wins; otherwise the repo must be present in the verified
# owners map. Never guess or fall back to a default owner string -- minting a
# registration token against the wrong owner silently orphans the runner
# (this is exactly how the colingreig -> ignitemarketing transfer broke
# registration in the first place).
resolve_runner_owner() {
  if [ -n "${HERMES_RUNNER_OWNER:-}" ]; then
    OWNER="$HERMES_RUNNER_OWNER"
    return 0
  fi
  if [ ! -f "$REGISTRATION" ]; then
    echo "[hermes-ci] cannot resolve owner for repo '$REPO': no HERMES_RUNNER_OWNER override and registration file missing at $REGISTRATION" >&2
    return 1
  fi
  local resolved
  resolved="$(jq -er --arg repo "$REPO" '.owners[$repo] // empty' "$REGISTRATION" 2>/dev/null)" || resolved=""
  if [ -z "$resolved" ]; then
    echo "[hermes-ci] cannot resolve owner for repo '$REPO': not present in $REGISTRATION and HERMES_RUNNER_OWNER is unset; refusing to guess an owner" >&2
    return 1
  fi
  OWNER="$resolved"
}

# App identity is read from the registration file (never runner-config.json --
# ci_health_watch.py's drift check does exact-dict-equality against that
# file's fixed 5-key operational schema, so adding App keys there would trip
# a false runner-config:drift alarm). Fail closed and name the exact file and
# key on any miss: an empty/absent App id or installation id must never let
# this script fall through to minting a token or calling the GitHub API.
resolve_runner_registration() {
  if [ ! -f "$REGISTRATION" ]; then
    echo "[hermes-ci] cannot resolve App registration: registration file missing at $REGISTRATION" >&2
    return 1
  fi
  APP_ID="$(jq -er '.github_app_id // empty' "$REGISTRATION" 2>/dev/null)" || APP_ID=""
  if [ -z "$APP_ID" ]; then
    echo "[hermes-ci] cannot resolve App registration: missing or empty github_app_id in $REGISTRATION" >&2
    return 1
  fi
  APP_INSTALL_ID="$(jq -er '.github_app_installation_id // empty' "$REGISTRATION" 2>/dev/null)" || APP_INSTALL_ID=""
  if [ -z "$APP_INSTALL_ID" ]; then
    echo "[hermes-ci] cannot resolve App registration: missing or empty github_app_installation_id in $REGISTRATION" >&2
    return 1
  fi
}

reconcile_own_lease() {
  local lease_boot lease_invocation lease_pid current_boot
  [ -f "$MARK" ] || return 0
  lease_boot="$(sed -n 's/^boot_id=//p' "$MARK" | head -n 1)"
  lease_invocation="$(sed -n 's/^invocation_id=//p' "$MARK" | head -n 1)"
  lease_pid="$(sed -n 's/^worker_pid=//p' "$MARK" | head -n 1)"
  current_boot="$(cat /proc/sys/kernel/random/boot_id)"
  if { [ -n "$lease_boot" ] && [ -n "$current_boot" ] && [ "$lease_boot" != "$current_boot" ]; } \
    || { [[ "$lease_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$lease_pid" 2>/dev/null; } \
    || { ! [[ "$lease_pid" =~ ^[0-9]+$ ]] && [ -n "$lease_invocation" ] && [ -n "${INVOCATION_ID:-}" ] && [ "$lease_invocation" != "$INVOCATION_ID" ]; }; then
    rm -f -- "$MARK"
  fi
}

archive_diagnostics() {
  local target_root="${1:?archive root required}"
  local archive_kind="${2:?archive kind required}"
  local stamp archive control_group
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="$target_root/${stamp}-$$"
  mkdir -p "$archive" || return 1
  chmod 0700 "$HERMES_CI" "$(dirname "$target_root")" "$target_root" "$archive" || return 1

  if [ -d "$DIR/_diag" ]; then
    cp -a "$DIR/_diag" "$archive/_diag" || return 1
  fi
  if [ -f "$RUNTIME_DIR/$NAME.env" ]; then
    cp "$RUNTIME_DIR/$NAME.env" "$archive/job.env" || return 1
  fi
  {
    printf 'repo=%s\nrunner_name=%s\narchive_kind=%s\narchived_at=%s\n' "$REPO" "$NAME" "$archive_kind" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'unit=hermes-runner@%s.service\n' "$REPO"
  } > "$archive/runner.env" || return 1

  journalctl -u "hermes-runner@$REPO.service" -n 500 --no-pager \
    > "$archive/journal.log" 2>&1 || true
  control_group="$(systemctl show "hermes-runner@$REPO.service" -p ControlGroup --value 2>/dev/null || true)"
  {
    printf 'control_group=%s\n' "$control_group"
    for file in memory.current memory.events memory.high memory.max memory.pressure cpu.pressure io.pressure; do
      if [ -n "$control_group" ] && [ -r "/sys/fs/cgroup${control_group}/$file" ]; then
        printf '\n[%s]\n' "$file"
        cat "/sys/fs/cgroup${control_group}/$file"
      fi
    done
    for file in /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
      if [ -r "$file" ]; then
        printf '\n[%s]\n' "$file"
        cat "$file"
      fi
    done
  } > "$archive/resources.txt" || return 1
  chmod -R go-rwx "$archive" || return 1
}

prune_archives() {
  local target_root="${1:?archive root required}"
  local days max count old
  days="$(jq -er '.diagnostic_retention_days | select(type == "number" and . == 7)' "$CONFIG")" || return 1
  max="$(jq -er '.diagnostic_max_jobs_per_repo | select(type == "number" and . == 20)' "$CONFIG")" || return 1
  find "$target_root" -mindepth 1 -maxdepth 1 -type d -mtime "+$days" -exec rm -rf -- {} +
  count="$(find "$target_root" -mindepth 1 -maxdepth 1 -type d -print | wc -l)"
  while [ "$count" -gt "$max" ]; do
    old="$(find "$target_root" -mindepth 1 -maxdepth 1 -type d -print | sort | head -n 1)"
    [ -n "$old" ] || break
    rm -rf -- "$old"
    count=$((count - 1))
  done
}

cleanup() {
  # Never destroy the only diagnostic copy. A failed archive leaves the work
  # tree intact for operator recovery and the next cycle exits fail closed.
  if has_job_evidence; then
    if archive_diagnostics "$ARCHIVE_ROOT" job && prune_archives "$ARCHIVE_ROOT"; then
      rm -rf -- "$DIR/_work" "$DIR/_diag"
      rm -f -- "$RUNTIME_DIR/$NAME.env"
    else
      echo "[hermes-ci] job diagnostic preservation failed; workdir retained" >&2
    fi
  elif has_diag_evidence; then
    if archive_diagnostics "$FAILURE_ARCHIVE_ROOT" registration-failure && prune_archives "$FAILURE_ARCHIVE_ROOT"; then
      rm -rf -- "$DIR/_work" "$DIR/_diag"
      rm -f -- "$RUNTIME_DIR/$NAME.env"
    else
      echo "[hermes-ci] failure diagnostic preservation failed; workdir retained" >&2
    fi
  else
    rm -rf -- "$DIR/_work" "$DIR/_diag"
    rm -f -- "$RUNTIME_DIR/$NAME.env" "$MARK"
  fi
  rm -f -- "$SLOTS_DIR/$NAME"
}
trap cleanup EXIT

cd "$DIR" || { echo "no runner dir $DIR"; exit 1; }

# Preserve anything left by a hard-killed cycle before clearing registration.
if [ -d "$DIR/_work" ] || [ -d "$DIR/_diag" ]; then
  if has_job_evidence; then
    archive_diagnostics "$ARCHIVE_ROOT" job || { echo "[hermes-ci] cannot preserve prior job cycle" >&2; exit 1; }
    prune_archives "$ARCHIVE_ROOT" || { echo "[hermes-ci] cannot enforce job diagnostic retention" >&2; exit 1; }
  elif has_diag_evidence; then
    archive_diagnostics "$FAILURE_ARCHIVE_ROOT" registration-failure || { echo "[hermes-ci] cannot preserve prior failure cycle" >&2; exit 1; }
    prune_archives "$FAILURE_ARCHIVE_ROOT" || { echo "[hermes-ci] cannot enforce failure diagnostic retention" >&2; exit 1; }
  fi
fi
rm -rf -- "$DIR/_work" "$DIR/_diag"
rm -f -- "$RUNTIME_DIR/$NAME.env"
rm -f -- "$DIR/.runner" "$DIR/.credentials" "$DIR/.credentials_rsaparams"
reconcile_own_lease

if [ "${HERMES_RUNNER_TEST_MODE:-0}" = "1" ]; then
  case "${HERMES_RUNNER_TEST_ACTION:-}" in
    cleanup)
      cleanup
      trap - EXIT
      exit 0
      ;;
    reconcile-lease)
      trap - EXIT
      exit 0
      ;;
    resolve-owner)
      trap - EXIT
      if resolve_runner_owner; then
        printf '%s\n' "$OWNER"
        exit 0
      fi
      exit 1
      ;;
    resolve-registration)
      trap - EXIT
      if resolve_runner_registration; then
        printf '%s %s\n' "$APP_ID" "$APP_INSTALL_ID"
        exit 0
      fi
      exit 1
      ;;
  esac
fi

resolve_runner_owner || exit 1
resolve_runner_registration || exit 1
[ -r "$APP_PEM" ] || { echo "missing executor App PEM at $APP_PEM"; exit 1; }

# Mint a short-lived (10m) hermes-executor installation token locally — same
# JWT construction as hermes-ops's scripts/lib/github-app-token.sh
# mint_app_token, reimplemented here so this VM never needs a live path to
# the mini's 1Password Connect (it already has direct internet access to
# api.github.com, proven by the registration-token call right below).
APP_TOKEN="$(python3 - "$APP_PEM" "$APP_ID" "$APP_INSTALL_ID" <<'PY'
import sys, time, json, urllib.request, urllib.error, base64, subprocess, tempfile, os
from pathlib import Path

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

pem_path, app_id, installation_id = sys.argv[1], sys.argv[2], sys.argv[3]
now = int(time.time())
header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
signing_input = f"{header}.{payload}".encode()
with tempfile.NamedTemporaryFile("wb", delete=False) as f:
    f.write(signing_input)
    msg_path = f.name
sig_path = msg_path + ".sig"
try:
    subprocess.check_call(
        ["openssl", "dgst", "-sha256", "-sign", pem_path, "-out", sig_path, msg_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sig = Path(sig_path).read_bytes()
finally:
    os.unlink(msg_path)
    if os.path.exists(sig_path):
        os.unlink(sig_path)
jwt = f"{header}.{payload}.{b64url(sig)}"
req = urllib.request.Request(
    f"https://api.github.com/app/installations/{installation_id}/access_tokens",
    data=b"{}",
    method="POST",
    headers={
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hermes-ci-runner-loop",
    },
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(json.load(r)["token"])
except urllib.error.HTTPError as e:
    sys.stderr.write(f"access_tokens mint failed: HTTP {e.code} {e.read().decode(errors='replace')}\n")
    sys.exit(1)
PY
)"
if [ -z "$APP_TOKEN" ]; then
  echo "App installation token mint failed for $REPO"
  exit 1
fi

REG_TOKEN="$(curl -fsS -X POST \
  -H "Authorization: token $APP_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$OWNER/$REPO/actions/runners/registration-token" \
  | jq -r .token)"
if [ -z "$REG_TOKEN" ] || [ "$REG_TOKEN" = "null" ]; then
  echo "registration-token mint failed for $REPO"
  exit 1
fi

./config.sh \
  --url "https://github.com/$OWNER/$REPO" \
  --token "$REG_TOKEN" \
  --labels self-hosted,linux,hermes-mini \
  --ephemeral --unattended --replace \
  --name "$NAME" || { echo "config.sh failed for $REPO"; exit 1; }

./run.sh
exit $?
