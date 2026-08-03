# Mac mini release cut — `scripts/mini-release-cut.sh`

Safe, repeatable release cut for the Hermes production Mac mini.

## Production-write lease

Before a real cut mutates release state, it acquires the registry-backed
`mini-release-cut` lease for `runtime-release` and `governed-mini-scripts`.
The cut uses the target tree's stdlib lease module directly, rather than the
`hermes` CLI, so an older active runtime can safely bootstrap this guard. The
lease is synchronously heartbeated during both 240-second service-verification
loops. Every guarded child operation also renews at completion inside the same
SQLite transaction, so a clone, dependency install, or reconciler that runs
past the 120-second TTL cannot commit an already-expired owner. The final lease
snapshot is included in the immutable release receipt and released by the cut
cleanup trap. A heartbeat refusal records immutable fence-loss evidence using
the preserved exact lease identity; the stale process retains its lock/lease
ownership evidence and performs no rollback or protected cleanup. The cut lock
is an exclusive owner file containing that exact lease identity. After an
evidence-backed lease recovery, only a live higher fencing token can prove the
old lease history and remove byte-identical stale owner metadata inside its own
mutation guard. Do not hand-remove the lock. Recover only an expired lease with
an operator evidence record.

## Why this exists

On **2026-07-19** an improvised cutover to a
`~/.hermes/releases/<ver>-<sha>/` + `~/.hermes/runtime-current` symlink layout
destroyed runtime state: SQLite DBs were truncated under live WAL connections,
and `config.yaml`, the auth token, and LaunchAgents were deleted. No committed
automation produced that layout, so it could not be reviewed or reproduced.

This script **is** that automation. It builds a brand-new release directory in
full, verifies it, and only then atomically repoints the `runtime-current`
symlink and restarts the services. Persistent runtime state remains untouched
except for the two explicit, rollback-safe governed deployments described
below: `clickup_workspace_refresh.py` and the canonical launchd environment.

Tracked in ClickUp `86e2ddah5`.

## Rebuilt fleet production policy

The rebuilt Hermes fleet has one production release path: an operator manually
runs the governed cutter with the exact certified full SHA and the immutable
promotion receipt ID that authorizes that same SHA:

```bash
~/.hermes/runtime-current/scripts/mini-release-cut.sh \
  --ref <certified-full-sha> \
  --certified-sha <same-certified-full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256>
```

The automatic LaunchAgent `com.colingreig.hermes.release-poll` is the fleet's
standing automatic release path, bootstrapped via
`install-mini-release-poller.sh --install-and-enable` (see below). It was
found silently unloaded on 2026-08-03 — nothing had alerted that it was
structurally absent rather than merely idle, which contributed to a
multi-day promotion logjam (ClickUp 86e2ky37p). It was reinstated the same
day, and `machine-setup/mini-scripts/fleet_outcome_contracts.json` now
declares it `expected: loaded` with a freshness contract against its own
heartbeat log (`~/.hermes/logs/mini-release-poll.log`, max age 2x
`StartInterval`), so an unloaded or non-executing poller pages independently
of whether a promotion happens to be waiting to cut.

## Layout on the mini

- Releases live at `~/.hermes/releases/v<version>-<12charsha>/` (a git clone +
  its own `venv/` + built `hermes_cli/web_dist/`).
- The active release is the `~/.hermes/runtime-current` symlink.
- `~/.hermes/releases/.previous` records the prior symlink target for rollback.
- Gateway: launchd `gui/501/ai.hermes.gateway` (API on `:8642`).
- Dashboard: launchd `gui/501/com.colingreig.hermes-dashboard` (`:9119`).
- `~/.local/bin/cu-clickup` is a release-managed, stable wrapper around the
  protected live `~/.hermes/scripts/clickup_workspace_refresh.py`;
  `/opt/homebrew/bin/cu-clickup` links to it so clean non-interactive shells
  can discover the command without depending on dotfile PATH setup.
- The canonical refresh source is
  `machine-setup/mini-scripts/clickup_workspace_refresh.py`. A successful cut
  atomically installs those exact bytes at the protected live path and records
  both SHA-256 values in a content-addressed receipt.
- The canonical launchd environment lives in
  `machine-setup/mini-scripts/` and is installed by
  `reconcile_launchd_environment.py`: the reconciler itself,
  source-identical wrappers/minter/resolver, a governed reference-only secret
  file merged from the complete validated `op-secrets.env` inventory plus
  required source-controlled keys, and generated gateway/dashboard plists.
  Those plists use the stable profile home as their working directory, pin the
  concrete active-release venv, and retain the core Aqua/Background plus
  25-second exit contract. Its content-addressed snapshot restores exact prior
  bytes on rollback.
- `.mini-release-last-receipt.json` is the stable latest receipt; immutable
  `.mini-release-receipt-<sha256>.json` siblings are addressed by their exact
  payload bytes. Successful explicit and automatic rollbacks record
  `event=rollback` only after both services verify. An equal-target no-op cites
  the exact prior full-activation receipt that authorizes it; a prior no-op is
  never accepted as activation proof.

## Usage

Run **on the mini** (over `ssh mini`). `node`/`npm` live in `/opt/homebrew/bin`,
which is not on a non-interactive ssh PATH — the script extends PATH itself.

```bash
# Standard cut of prod-live-patches:
~/.hermes/runtime-current/scripts/mini-release-cut.sh \
  --ref <certified-full-sha> \
  --certified-sha <same-certified-full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256>

# Preview every mutating action, change nothing:
~/.hermes/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches --dry-run

# Generic contingency only: equal is a no-op; only a strict descendant cuts:
~/.hermes/runtime-current/scripts/mini-release-cut.sh \
  --ref <certified-full-sha> \
  --certified-sha <same-certified-full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256> \
  --if-advanced

# Cut a specific sha or branch:
~/.hermes/runtime-current/scripts/mini-release-cut.sh \
  --ref <certified-full-sha> \
  --certified-sha <same-certified-full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256>

# Roll back to the previous release (no build):
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback

# Cut, then prune down to active + previous release only (disk lifecycle, 86e2k3ryc;
# set MINI_RELEASE_KEEP_EXTRA=N to keep N additional older releases instead):
~/.hermes/runtime-current/scripts/mini-release-cut.sh \
  --ref <certified-full-sha> \
  --certified-sha <same-certified-full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256> \
  --prune
```

`--ref` defaults to `prod-live-patches`.
Every real cut and preflight requires both `--certified-sha` and
`--promotion-receipt-id`. The resolved target must equal the full certified SHA
exactly, and the immutable promotion receipt must authorize that same target;
ancestry is not accepted as certification. Dry-runs and explicit rollback are
the only exceptions. `MINI_RELEASE_VERIFY_TIMEOUT` accepts only a plain decimal
integer from 1 through 900; `MINI_RELEASE_KEEP_EXTRA` accepts only 0 through 20.
Signs, whitespace, leading-zero/octal forms, shell syntax, and larger values
fail before release-state work.

## One-time stale-cutter bootstrap

Use this bridge only for the first deployment of a cutter/lease repair when
`runtime-current` still contains the older cutter. Normal releases must use the
active governed cutter shown above. In particular, do not invoke the stale
active cutter for this first cut, and do not retry
`7ac5f920bef287f5968237191f7fe46313cdaf84` or reuse its existing
`v0.18.2-7ac5f920bef2` directory. Promote a new strict-descendant repair commit
so the cutter can build a new immutable release directory.

Preparation must not fetch or mutate the active clone before the production
lease exists. This procedure fetches `main`, `prod-live-patches`, and the
promotion blob into a private bare repository under the fixed canonical
`/private/tmp` root. The repaired cutter later performs the only active-clone
fetch, after it has acquired the lease.

The bootstrap override changes only where the cutter imports its target lease
module. Promotion verification remains deliberately hardwired to the trusted
active runtime's certifier. Before any target bytes execute, this procedure
therefore proves that temporary `main` and `prod-live-patches` both resolve to
the exact target, hashes the fetched promotion blob to its receipt ID, proves
the on-disk active certifier is byte-identical to the freshly fetched
`ACTIVE_SHA` certifier blob, proves the target certifier is identical to both,
and only then runs that trusted active certifier against the target and
receipt. The entire trust, extraction, execution, cleanup, and post-assertion
block runs inside one non-profile `env -i` boundary with a fixed PATH and
`PYTHONNOUSERSITE=1`; ambient shell, Python-user-site, and tar options cannot
enter it. Run this as one Bash session on the Mini, replacing only the two
authority values:

```bash
/usr/bin/env -i \
  HOME=/Users/colingreig \
  USER=colingreig \
  LOGNAME=colingreig \
  SHELL=/bin/zsh \
  LC_ALL=C \
  TMPDIR=/private/tmp \
  PATH=/opt/homebrew/bin:/Users/colingreig/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  PYTHONNOUSERSITE=1 \
  HERMES_HOME=/Users/colingreig/.hermes \
  /bin/bash <<'HERMES_MINI_RELEASE_BOOTSTRAP'
set -euo pipefail
umask 077

MINI_HOME=/Users/colingreig
HERMES_ROOT=/Users/colingreig/.hermes
ACTIVE_LINK="$HERMES_ROOT/runtime-current"
RELEASES_DIR="$HERMES_ROOT/releases"
HERMES_BIN=/Users/colingreig/.local/bin/hermes
ORIGIN_URL=https://github.com/colingreig/hermes-agent-upstream-contrib.git
TARGET_SHA='<new-promoted-repair-full-sha>'
PROMOTION_RECEIPT_ID='<promotion-receipt-sha256>'

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$PROMOTION_RECEIPT_ID" =~ ^[0-9a-f]{64}$ ]]
[ "$TARGET_SHA" != 7ac5f920bef287f5968237191f7fe46313cdaf84 ]
[ -L "$ACTIVE_LINK" ]
ACTIVE_DIR="$(readlink "$ACTIVE_LINK")"
case "$ACTIVE_DIR" in "$RELEASES_DIR"/v*) ;; *) exit 2 ;; esac
[ -d "$ACTIVE_DIR" ] && [ ! -L "$ACTIVE_DIR" ]
ACTIVE_SHA="$(/usr/bin/git -C "$ACTIVE_DIR" rev-parse --verify 'HEAD^{commit}')"
[ "$(/usr/bin/git -C "$ACTIVE_DIR" remote get-url origin)" = "$ORIGIN_URL" ]
ACTIVE_PYTHON="$ACTIVE_DIR/venv/bin/python"
ACTIVE_CERTIFIER="$ACTIVE_DIR/scripts/certify_prod_live_patches.py"
[ -x "$ACTIVE_PYTHON" ]
[ -f "$ACTIVE_CERTIFIER" ] && [ ! -L "$ACTIVE_CERTIFIER" ]

MINI_USER="$(id -un)"
[ "$MINI_USER" = colingreig ]
CLEAN_ENV=(
  /usr/bin/env -i
  HOME="$MINI_HOME"
  USER="$MINI_USER"
  LOGNAME="$MINI_USER"
  SHELL=/bin/zsh
  LC_ALL=C
  TMPDIR=/private/tmp
  PATH=/opt/homebrew/bin:/Users/colingreig/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
  PYTHONNOUSERSITE=1
  HERMES_HOME="$HERMES_ROOT"
)

# The literal /private/tmp prefix is part of the safety boundary. Do not use
# ambient TMPDIR here.
BOOTSTRAP_ROOT="$(/usr/bin/mktemp -d /private/tmp/hermes-mini-release-bootstrap.XXXXXX)"
[[ "$BOOTSTRAP_ROOT" =~ ^/private/tmp/hermes-mini-release-bootstrap\.[A-Za-z0-9]{6}$ ]]
[ -d "$BOOTSTRAP_ROOT" ] && [ ! -L "$BOOTSTRAP_ROOT" ]
[ "$(cd -P "$BOOTSTRAP_ROOT" && pwd -P)" = "$BOOTSTRAP_ROOT" ]
[ "$(/usr/bin/stat -f '%u' "$BOOTSTRAP_ROOT")" = "$(/usr/bin/id -u)" ]
readonly BOOTSTRAP_ROOT

cleanup_bootstrap() {
  local status=$? canonical="" lease_status="" active_count=""
  trap - EXIT
  if [ ! -d "$BOOTSTRAP_ROOT" ] || [ -L "$BOOTSTRAP_ROOT" ] ||
     [ "$(/usr/bin/stat -f '%u' "$BOOTSTRAP_ROOT" 2>/dev/null || true)" != "$(/usr/bin/id -u)" ]; then
    printf 'preserving unsafe/unverifiable bootstrap root: %s\n' "$BOOTSTRAP_ROOT" >&2
    [ "$status" -ne 0 ] || status=70
    exit "$status"
  fi
  canonical="$(cd -P "$BOOTSTRAP_ROOT" && pwd -P)" || canonical=""
  if [ "$canonical" != "$BOOTSTRAP_ROOT" ] ||
     ! [[ "$canonical" =~ ^/private/tmp/hermes-mini-release-bootstrap\.[A-Za-z0-9]{6}$ ]]; then
    printf 'preserving unexpected bootstrap root: %s\n' "$BOOTSTRAP_ROOT" >&2
    [ "$status" -ne 0 ] || status=70
    exit "$status"
  fi
  if /usr/bin/pgrep -f -- "$BOOTSTRAP_ROOT/payload/scripts/mini-release-cut.sh" >/dev/null; then
    printf 'preserving bootstrap root referenced by a live cutter: %s\n' "$BOOTSTRAP_ROOT" >&2
    [ "$status" -ne 0 ] || status=70
    exit "$status"
  fi
  if ! lease_status="$("${CLEAN_ENV[@]}" "$HERMES_BIN" production-write-lease status)" ||
     ! active_count="$(printf '%s' "$lease_status" | /usr/bin/python3 -c \
       'import json,sys; print(len(json.load(sys.stdin)["active_leases"]))')" ||
     [ "$active_count" != 0 ]; then
    printf 'preserving bootstrap root while a lease is active or unverifiable: %s\n' \
      "$BOOTSTRAP_ROOT" >&2
    [ "$status" -ne 0 ] || status=70
    exit "$status"
  fi
  /bin/rm -rf -- "$BOOTSTRAP_ROOT"
  exit "$status"
}
trap cleanup_bootstrap EXIT

SOURCE_REPO="$BOOTSTRAP_ROOT/source.git"
PAYLOAD_DIR="$BOOTSTRAP_ROOT/payload"
PROMOTION_RECEIPT="$BOOTSTRAP_ROOT/promotion-receipt.json"
"${CLEAN_ENV[@]}" /usr/bin/git init --bare "$SOURCE_REPO"
"${CLEAN_ENV[@]}" /usr/bin/git -c gc.auto=0 -c maintenance.auto=false \
  -C "$SOURCE_REPO" fetch --no-tags \
  "$ORIGIN_URL" \
  '+refs/heads/main:refs/bootstrap/main' \
  '+refs/heads/prod-live-patches:refs/bootstrap/prod-live-patches' \
  "+refs/tags/prod-live-patches-promotion-${PROMOTION_RECEIPT_ID}:refs/bootstrap/promotion"

MAIN_SHA="$("${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" rev-parse --verify 'refs/bootstrap/main^{commit}')"
PROD_SHA="$("${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" rev-parse --verify 'refs/bootstrap/prod-live-patches^{commit}')"
[ "$MAIN_SHA" = "$TARGET_SHA" ]
[ "$PROD_SHA" = "$TARGET_SHA" ]
[ "$TARGET_SHA" != "$ACTIVE_SHA" ]
"${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" merge-base --is-ancestor "$ACTIVE_SHA" "$TARGET_SHA"
[ "$("${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" cat-file -t refs/bootstrap/promotion)" = blob ]
"${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" cat-file blob \
  refs/bootstrap/promotion > "$PROMOTION_RECEIPT"
[ "$(shasum -a 256 "$PROMOTION_RECEIPT" | awk '{print $1}')" = \
  "$PROMOTION_RECEIPT_ID" ]

CERTIFIER_REL=scripts/certify_prod_live_patches.py
FETCHED_ACTIVE_CERTIFIER="$BOOTSTRAP_ROOT/active-certifier.py"
"${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" show \
  "${ACTIVE_SHA}:${CERTIFIER_REL}" > "$FETCHED_ACTIVE_CERTIFIER"
chmod 0600 "$FETCHED_ACTIVE_CERTIFIER"
FETCHED_ACTIVE_CERTIFIER_HASH="$(shasum -a 256 "$FETCHED_ACTIVE_CERTIFIER" | awk '{print $1}')"
ACTIVE_CERTIFIER_HASH="$(shasum -a 256 "$ACTIVE_CERTIFIER" | awk '{print $1}')"
[ "$ACTIVE_CERTIFIER_HASH" = "$FETCHED_ACTIVE_CERTIFIER_HASH" ]
TARGET_CERTIFIER_HASH="$(
  "${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" show "${TARGET_SHA}:${CERTIFIER_REL}" |
    shasum -a 256 | awk '{print $1}'
)"
[ "$TARGET_CERTIFIER_HASH" = "$FETCHED_ACTIVE_CERTIFIER_HASH" ]
"${CLEAN_ENV[@]}" "$ACTIVE_PYTHON" "$ACTIVE_CERTIFIER" verify-receipt \
  --receipt "$PROMOTION_RECEIPT" \
  --receipt-id "$PROMOTION_RECEIPT_ID" \
  --head-sha "$TARGET_SHA"

TARGET_VERSION="$(
  "${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" show "${TARGET_SHA}:pyproject.toml" |
    "$ACTIVE_PYTHON" -c \
      'import sys,tomllib; print(tomllib.load(sys.stdin.buffer)["project"]["version"])'
)"
TARGET_DIR="$RELEASES_DIR/v${TARGET_VERSION}-${TARGET_SHA:0:12}"
case "$TARGET_DIR" in "$RELEASES_DIR"/v*) ;; *) exit 2 ;; esac
[ ! -e "$TARGET_DIR" ] && [ ! -L "$TARGET_DIR" ]

TARGET_FILES=(
  scripts/mini-release-cut.sh
  cron/production_write_lease.py
  hermes_constants.py
  machine-setup/production_mutation_registry.json
)
mkdir "$PAYLOAD_DIR"
"${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" archive \
  "$TARGET_SHA" "${TARGET_FILES[@]}" | /usr/bin/tar -x -C "$PAYLOAD_DIR"
: > "$PAYLOAD_DIR/cron/__init__.py"
chmod 0700 "$PAYLOAD_DIR" "$PAYLOAD_DIR/scripts" \
  "$PAYLOAD_DIR/cron" "$PAYLOAD_DIR/machine-setup"
chmod 0700 "$PAYLOAD_DIR/scripts/mini-release-cut.sh"
chmod 0600 "$PAYLOAD_DIR/cron/production_write_lease.py" \
  "$PAYLOAD_DIR/cron/__init__.py" \
  "$PAYLOAD_DIR/hermes_constants.py" \
  "$PAYLOAD_DIR/machine-setup/production_mutation_registry.json"

for relative in "${TARGET_FILES[@]}"; do
  [ -f "$PAYLOAD_DIR/$relative" ] && [ ! -L "$PAYLOAD_DIR/$relative" ]
  [ "$(stat -f '%u' "$PAYLOAD_DIR/$relative")" = "$(id -u)" ]
  expected="$(
    "${CLEAN_ENV[@]}" /usr/bin/git -C "$SOURCE_REPO" show "${TARGET_SHA}:${relative}" |
      shasum -a 256 | awk '{print $1}'
  )"
  actual="$(shasum -a 256 "$PAYLOAD_DIR/$relative" | awk '{print $1}')"
  [ "$actual" = "$expected" ]
done
[ ! -s "$PAYLOAD_DIR/cron/__init__.py" ]

CUTTER="$PAYLOAD_DIR/scripts/mini-release-cut.sh"
"${CLEAN_ENV[@]}" \
  HERMES_PRODUCTION_WRITE_LEASE_BOOTSTRAP_DIR="$PAYLOAD_DIR" \
  "$CUTTER" --preflight \
    --ref "$TARGET_SHA" \
    --certified-sha "$TARGET_SHA" \
    --promotion-receipt-id "$PROMOTION_RECEIPT_ID"

# Continue synchronously with the same hash-verified cutter. env -i deliberately
# excludes test-library, timeout, keep-extra, prune, and every other ambient
# MINI_RELEASE_* override.
"${CLEAN_ENV[@]}" \
  HERMES_PRODUCTION_WRITE_LEASE_BOOTSTRAP_DIR="$PAYLOAD_DIR" \
  "$CUTTER" --if-advanced \
    --ref "$TARGET_SHA" \
    --certified-sha "$TARGET_SHA" \
    --promotion-receipt-id "$PROMOTION_RECEIPT_ID"

[ "$(readlink "$ACTIVE_LINK")" = "$TARGET_DIR" ]
[ "$(/usr/bin/git -C "$ACTIVE_LINK" rev-parse --verify 'HEAD^{commit}')" = "$TARGET_SHA" ]
for relative in "${TARGET_FILES[@]}"; do
  cmp -s "$PAYLOAD_DIR/$relative" "$ACTIVE_LINK/$relative"
done

GATEWAY_HEALTH="$(
  "${CLEAN_ENV[@]}" /usr/bin/curl --fail --silent --show-error --max-time 10 \
    http://127.0.0.1:8642/health
)"
printf '%s' "$GATEWAY_HEALTH" | "${CLEAN_ENV[@]}" "$ACTIVE_LINK/venv/bin/python" -c \
  'import json,sys; value=json.load(sys.stdin); assert value["status"] == "ok"; assert value["platform"] == "hermes-agent"'
"${CLEAN_ENV[@]}" /usr/bin/curl --fail --silent --show-error --max-time 10 \
  --output /dev/null http://127.0.0.1:9119/

LEASE_STATUS="$("${CLEAN_ENV[@]}" "$HERMES_BIN" production-write-lease status)"
printf '%s' "$LEASE_STATUS" | /usr/bin/python3 -c \
  'import json,sys; assert json.load(sys.stdin)["active_leases"] == []'
[ ! -e "$RELEASES_DIR/.mini-release-cut.lock" ] &&
  [ ! -L "$RELEASES_DIR/.mini-release-cut.lock" ]

LAST_RECEIPT="$RELEASES_DIR/.mini-release-last-receipt.json"
[ -f "$LAST_RECEIPT" ] && [ ! -L "$LAST_RECEIPT" ]
RECEIPT_ID="$(shasum -a 256 "$LAST_RECEIPT" | awk '{print $1}')"
ADDRESSED_RECEIPT="$RELEASES_DIR/.mini-release-receipt-${RECEIPT_ID}.json"
[ -f "$ADDRESSED_RECEIPT" ] && [ ! -L "$ADDRESSED_RECEIPT" ]
cmp -s "$LAST_RECEIPT" "$ADDRESSED_RECEIPT"

"${CLEAN_ENV[@]}" "$ACTIVE_LINK/venv/bin/python" - \
  "$LAST_RECEIPT" "$ACTIVE_SHA" "$TARGET_SHA" "$TARGET_DIR" \
  "$PROMOTION_RECEIPT_ID" "$HERMES_ROOT" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

receipt_path, active_sha, target_sha, target_dir, promotion_id, hermes_root = sys.argv[1:]
payload = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
digest = re.compile(r"^[0-9a-f]{64}$")
lease = payload.get("production_write_lease")
assert payload["schema_version"] == 2
assert payload["event"] == "advanced"
assert payload["from_commit"] == active_sha
assert payload["to_commit"] == target_sha
assert payload["certified_source_commit"] == target_sha
assert payload["promotion_authority_receipt_id"] == promotion_id
assert payload["runtime_target"] == target_dir
assert payload["review_poll_gate_smoke"] == "passed"
assert digest.fullmatch(payload["pr_pipeline_reconciliation_receipt_id"])
assert payload["prior_full_activation_receipt_id"] is None
assert isinstance(payload.get("detail"), str) and payload["detail"]
assert isinstance(lease, dict) and lease["actor"] == "mini-release-cut"
assert set(lease["resources"]) == {"runtime-release", "governed-mini-scripts"}
assert lease["commit_sha"] == target_sha
assert isinstance(lease.get("lease_id"), str) and lease["lease_id"]
assert isinstance(lease.get("fencing_token"), int) and lease["fencing_token"] > 0
source = Path(target_dir) / "machine-setup/mini-scripts/clickup_workspace_refresh.py"
deployed = Path(hermes_root) / "scripts/clickup_workspace_refresh.py"
source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
deployed_hash = hashlib.sha256(deployed.read_bytes()).hexdigest()
assert source_hash == deployed_hash == payload["refresh_source_sha256"]
assert payload["refresh_deployed_sha256"] == deployed_hash
PY
HERMES_MINI_RELEASE_BOOTSTRAP
```

Every assertion is a stop condition. If preflight or the cut exits nonzero, do
not switch to the stale cutter, retry the same SHA, delete a partial release,
or recover a non-expired lease. Preserve the command output and inspect
`hermes production-write-lease status`, the immutable recovery/fence-loss
receipts, `runtime-current`, `.previous`, and the latest release receipt before
any operator recovery. The trap removes only the canonical, current-user-owned,
non-symlink fixed-prefix bootstrap root, and only after proving no cutter
process or active production-write lease still references the operation. If
those checks cannot be proved, it preserves the directory and exits nonzero.

## Release-poll LaunchAgent — installer and governed controls

`com.colingreig.hermes.release-poll` (see "Rebuilt fleet production policy"
above — it is installed, enabled, and the fleet's standing automatic release
path, not an opt-in contingency) is managed with the same installer script.
This section previously described the LaunchAgent as "not installed or
enabled for the rebuilt Hermes fleet," which stopped being accurate once the
poller was reinstated on 2026-08-03 (#310) — that PR corrected the "retired"
language earlier in this file but missed this second, contradicting
occurrence. The poller is local-only, exposes no webhook, and delegates all
decisions to the locked cutter:

```bash
~/.hermes/runtime-current/scripts/install-mini-release-poller.sh --install \
  --certified-sha <full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256>
# Review the installed plist, then explicitly load it:
~/.hermes/runtime-current/scripts/install-mini-release-poller.sh --install-and-enable \
  --actor "<operator-id>" --reason "<exact-SHA CI evidence>" \
  --certified-sha <full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256>

# Governed controls used during rollout:
~/.hermes/runtime-current/scripts/install-mini-release-poller.sh --freeze \
  --actor "<operator-id>" --reason "<why>"
~/.hermes/runtime-current/scripts/install-mini-release-poller.sh --unfreeze \
  --actor "<operator-id>" --reason "<exact-SHA CI evidence>" \
  --certified-sha <full-sha> \
  --promotion-receipt-id <promotion-receipt-sha256>
~/.hermes/runtime-current/scripts/install-mini-release-poller.sh --status
```

The installer first runs the cutter's dedicated `--preflight` mode. Preflight
acquires the real release lock, fetches current origin metadata, and refuses a
behind or diverged branch without building, switching, restarting services, or
writing a receipt. The LaunchAgent runs every 15 minutes and is not `RunAtLoad`.
Poll-control changes are content-addressed, persist under `releases/`, and
require an explicit actor and reason. Missing, corrupt, locked, or
unsafe-permission control state prevents polling. A failed managed cut records
a frozen receipt, or remains fail-closed if the control itself is unavailable.

**Deferral is not failure (ClickUp 86e2md9ck).** Before switching the runtime
the cutter arms the gateway's reversible external drain and waits
`MINI_RELEASE_DRAIN_TIMEOUT` (default 300s) for `gateway_state=draining` +
`active_agents=0`. A normal cron agent run is 25-45 minutes, so that window
routinely expires simply because the fleet is working. When it does — and only
when the drain marker came back off cleanly and nothing was switched — the
cutter logs `release cut deferred: gateway did not quiesce ...`, exits **75**
(`EX_TEMPFAIL`), and deliberately leaves poll-control **unfrozen**: the next
15-minute poll retries unattended against the same certified SHA and promotion
receipt, re-running every fetch, promotion-authority, advancement, build, and
verify gate from scratch. `mini-release-poll.sh` maps that 75 to its own
`cut deferred by a busy fleet` line and a clean exit.

Everything else keeps the original fail-closed behaviour: a drain that could
not be *armed*, a marker that could not be *cleared*, and every build/verify
failure still freeze poll-control for operator reconciliation. Because an
unattended retry is silent by design, the `release-cut-drain-deferral`
fleet-outcome contract counts deferral lines in
`~/.hermes/logs/mini-release-poll.log` and alarms at 6 inside 240 minutes, so a
fleet that can *never* quiesce pages instead of quietly never deploying.

The drain window is intentionally NOT widened to cover a whole agent run: the
marker refuses new fleet work for as long as it is armed, so a 45-minute
blocking wait inside a 15-minute cron would park the fleet in drain
permanently. Short window plus cheap automatic retry is the contract.

Any deployment that explicitly adopts this contingency must first prove its
promotion branch contains the active runtime commit; the locked preflight
accepts only an equal or strict-descendant target and rejects all unverifiable
authority.

## What a cut does (order matters)

1. `git -C ~/.hermes/runtime-current fetch --prune origin` — fetch in the
   **current** clone (no new dir yet).
2. Resolve the target commit (`origin/<ref>` or a raw sha) and read the
   `[project]` version from `pyproject.toml` **at that commit**.
3. Name the new dir `releases/v<version>-<12charsha>`. **Refuse if it already
   exists** (no in-place mutation).
4. Build **entirely in the new dir**: full network clone from the real
   `origin` URL (the default; it avoids inheriting missing blobs from the
   blobless `runtime-current` clone), detached-checkout the sha, build the venv
   (`uv sync --extra all --locked`, falling back to `uv venv` + editable pip),
   build the web dist (`npm ci --include=dev && npm run build --workspace web` →
   `hermes_cli/web_dist/`). `--offline` is the explicit, best-effort local
   clone fallback; its integrity check must pass before it can be activated.
5. **Verify the build before any switch**: `venv/bin/python -c "import
   hermes_cli.main"`, `hermes_cli/web_dist/index.html` present, and the governed
   refresh and launchd Python/shell sources compile.
6. Back up the exact currently deployed refresh bytes under `releases/`, then
   record the current symlink target to `releases/.previous`.
7. **Atomic switch**: `ln -sfn` a temp symlink + `mv -fh` over
   `runtime-current`.
8. Transactionally reconcile both LaunchAgents and their canonical wrappers,
   then reload with `bootout` + `bootstrap`. Plists point only to wrappers;
   permanent auth parks cleanly while transient exhaustion remains retryable.
9. Reconcile the PR-pipeline manifest from the newly selected release at the
   exact certified SHA. The reconciler records a composite receipt and runs the
   deployed root `review_poll_gate.py` command using the release Python from
   `/` under a sanitized environment.
10. **Verify (up to 240s, with synchronous lease renewal)**: gateway process running through the new release,
   `Gateway running with N platform(s)` with N ≥ 2 after the restart's captured
   `gateway.log` byte boundary, and `:8642` listening; then verify the dashboard
   (`:9119` → HTTP 200). Equal-target health derives its boundary from the
   exact live `gateway.pid` PID/start fingerprint and process creation time;
   accumulated readiness from an older gateway start cannot certify it, and an
   unverifiable or rotated-away current-start boundary fails closed.
11. Atomically install and hash-verify the governed refresh source at
   `~/.hermes/scripts/clickup_workspace_refresh.py`, then install (or repair)
   the executable `~/.local/bin/cu-clickup` wrapper and PATH link.
12. Record a deterministic receipt with old/new commits, certified SHA,
    promotion receipt ID, runtime target, reconciliation receipt, review-gate
    smoke, source hash, deployed hash, ref, event, and result detail.
13. On **any** verification, governed install, CLI install, hash, or receipt
    failure: **automatic rollback** of the runtime target, launchd snapshot, and
    governed refresh bytes, restart, re-verify, write an immutable rollback
    receipt, rotate `.previous` to the failed/new generation so the reversal is
    itself reversible, preserve that generation from failure cleanup, and exit
    non-zero. Failed or partially verified rollback never rotates the
    generation or writes a successful rollback receipt.

## Hard safety invariants (enforced in code, not comments)

1. The build only ever writes **under `~/.hermes/releases/`**. Each release
   target is reconstructed from a canonical `releases/` parent, and immediately
   before every create or removal that resolved parent must equal `releases/`.
   Version strings must be ASCII PEP 440-safe components: they begin with a
   decimal digit and may contain only letters, digits, `.`, `!`, `+`, `_`, and
   `-`; whitespace, controls, slashes, shell punctuation, and option-looking
   values are rejected.
2. After the exact target-bound production lease is acquired,
   `git fetch --prune origin` deliberately updates **Git metadata only** in the
   existing `runtime-current` clone; it never changes that clone's checked-out
   worktree or live runtime state. The other
   out-of-`releases/` actions are (a) the atomic `runtime-current` symlink
   repoint, (b) the `launchctl` restart, and (c) the atomic replacement of the
   managed command `~/.local/bin/cu-clickup` plus its
   `/opt/homebrew/bin/cu-clickup` discovery link, each funnelled through
   dedicated functions. The governed operational replacements are
   exact-path-only, refuse symlink source/destination paths, stage and hash
   in the destination directory, and atomically rename over only the declared
   refresh/launchd assets. The ClickUp wrapper contains no token and reads
   credentials from the environment only when it invokes that script.
3. It **never** touches `~/.hermes/{config.yaml,*.db,cron/,logs/,recovery/}` or
   `~/.config`. `~/.hermes/scripts/` and `~/Library/LaunchAgents` remain
   forbidden to generic writes; only the exact governed refresh and launchd
   reconciler target sets are exceptions. Logs remain read-only.
4. It **refuses to run** if the target release dir already exists — never
   mutates a release in place.
5. It **refuses to bootstrap** a missing `runtime-current` symlink or
   `releases/` dir from scratch (that improvisation is what caused the incident).
6. The symlink swap is **atomic** (`ln -sfn` temp + `mv -fh` rename). `-h`
   prevents BSD `mv` from following an existing symlink-to-directory.
7. `.previous` (under `releases/`) records the rollback target; failed
   verification auto-rolls-back to it.
8. Pruning keeps only the **active (runtime-current target) + previous**
   release by default (disk lifecycle, 86e2k3ryc; set `MINI_RELEASE_KEEP_EXTRA`
   to retain N additional older releases) and **only runs on explicit
   `--prune`** — never by default, and never removes the active or previous
   release.
9. `--dry-run` prints every mutating action and performs none.
10. A regular, mode-0600 `releases/.mini-release-cut.lock` owner file is linked
    into place exclusively inside the mutation guard for the full cut,
    rollback, or prune operation. Its JSON records the exact lease owner and
    fencing token. Stale cleanup requires a live later release lease, exact
    released/recovered predecessor history, and guarded byte-for-byte removal;
    live, malformed, symlinked, unsafe-mode, same-token, or newer-owner locks
    fail closed. The one-time legacy empty-directory lock migration is also
    governed: a current higher cutter lease must uniquely bind the directory
    mtime to one terminal/recovered predecessor lease, and the guarded rmdir
    rechecks a non-symlink, current-user-owned, safe-mode, empty inode with the
    exact same mtime and no other cutter process. Ambiguous, live, non-empty,
    changed, or otherwise unsafe directories fail closed; never `rm` one by
    hand.
11. `--if-advanced` resolves and classifies the ref while holding that same
    lock. Equal commits emit a content-addressed no-op receipt only when an
    existing immutable exact-target `cut`, `advanced`, or `rollback` receipt
    proves the complete activation gate set; that receipt's lease must bind the
    target SHA. Historical proof alone is insufficient: current source/deployed
    governed hashes, runtime symlink and Git SHA, gateway process/platform/port
    and semantic HTTP health, and dashboard HTTP health must all pass before a
    preflight or no-op receipt. Platform readiness must occur after the exact
    live gateway process's current-start log boundary; older accumulated log
    lines are not evidence. Drift or an unverifiable boundary writes no no-op.
    An equal symlink left by a partial/failed cut fails closed. Only a strict
    descendant can cut; behind, diverged, or unresolvable ancestry fails closed.
12. Receipt filenames are the SHA-256 of their canonical JSON payload. Repeated
    polls in identical state reuse the same immutable receipt.
13. Every non-dry-run cut or preflight requires an exact full certified SHA and
    a lowercase SHA-256 promotion receipt ID. The resolved target must equal
    the certified SHA, and the immutable receipt must authorize that exact
    target before any build or switch. The production lease is acquired against
    that already-supplied target SHA, which binds its mutation snapshots,
    activation receipt, and any fence-loss evidence to the target rather than
    the old active HEAD. Explicit rollback instead binds the current active SHA.

## Rollback

```bash
~/.hermes/runtime-current/scripts/mini-release-cut.sh --rollback
```

Repoints `runtime-current` to the release recorded in `releases/.previous`,
atomically restores that release's governed refresh source (or the staged
pre-vendor bytes for the bootstrap cut), restores the exact prior launchd
snapshot and managed CLI, restarts both services, and re-verifies. No build. If
restoration or either service does not verify healthy it exits non-zero and
asks for manual intervention rather than looping. Both service kickstarts run
inside the exact lease mutation guard. After every fully verified explicit or
automatic rollback, `.previous` is atomically rotated to the former active
release so the action is reversible, then a content-addressed `event=rollback`
receipt is written. The failure cleanup preserves that rollback generation.
