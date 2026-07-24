#!/usr/bin/env python3
"""Nightly off-box restic backup for ~/.hermes to Cloudflare R2.

This script is intentionally self-contained so launchd can run it directly.
It resolves the R2 credentials from the 1Password service-account SDK via
`op_sdk_resolve.py`, then runs restic against the authoritative Hermes state
on the mini.

Modes:
  backup         Ensure repo exists, run backup, apply retention.
  init           Ensure the restic repo exists.
  check          Verify the repo is reachable.
  snapshots      Print JSON snapshots for the current repo.
  restore-test   Restore ~/.hermes/config.yaml to a scratch dir and diff it.

Exit code:
  0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from op_sdk_resolve import resolve_all_fields

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
BACKUP_ITEM_VAULT = "hermes-agent"
BACKUP_ITEM_TITLE = "Hermes Backup — Cloudflare R2 (restic)"
SLACK_TARGET = os.environ.get("HERMES_BACKUP_SLACK_TARGET", "slack:D0BA2PM9CFM")
HERMES_BIN = os.environ.get("HERMES_BIN") or str(Path(sys.executable).with_name("hermes")) or shutil.which("hermes") or str(HOME / ".local/bin/hermes")
DEFAULT_RESTIC = os.environ.get("RESTIC_BIN") or shutil.which("restic") or "/opt/homebrew/bin/restic"
DEFAULT_HOST = os.environ.get("HERMES_BACKUP_HOST", os.uname().nodename.split(".")[0])
LOG_PREFIX = "[offbox-restic-backup]"

# Priority order from the ClickUp task.
BACKUP_TARGETS = [
    HERMES_HOME / "config.yaml",
    HERMES_HOME / "cron" / "jobs.json",
    HERMES_HOME / "scripts",
    HERMES_HOME / "hermes-agent",
    HERMES_HOME / "state.db",
    HERMES_HOME / "db-backups",
    HERMES_HOME / "kanban.db",
    HOME / "Library" / "LaunchAgents",
    HOME / ".config" / "op-repo-vault-map.tsv",
    HOME / ".config" / "opencode" / "opencode.jsonc",
    HERMES_HOME / "recovery",
    # Added 2026-07-22 (ClickUp 86e2e870p): ~/.hermes/memories was never in
    # scope, so the 2026-07-19 home-directory wipe permanently lost Hermes's
    # entire MEMORY.md/USER.md personalization with no snapshot to restore
    # from, at any point in restic's history. This closes that gap going
    # forward; it does not recover what was already lost.
    HERMES_HOME / "memories",
]

# Explicitly avoid mirroring the most sensitive material unless it is equally
# protected elsewhere. The task called out these paths by name.
EXCLUDES = [
    str(HERMES_HOME / "secrets"),
    str(HERMES_HOME / "secrets" / "**"),
    str(HERMES_HOME / "scripts" / "op-secrets.env*"),
    str(HERMES_HOME / "scripts" / "**" / "op-secrets.env*"),
]

RETENTION = {
    "daily": 7,
    "weekly": 4,
    "monthly": 6,
}


@dataclass(frozen=True)
class BackupEnv:
    repo: str
    aws_access_key_id: str
    aws_secret_access_key: str
    restic_password: str


class BackupError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}")


def eprint(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr)


def restic_bin() -> str:
    if os.path.isabs(DEFAULT_RESTIC) and os.path.exists(DEFAULT_RESTIC):
        return DEFAULT_RESTIC
    resolved = shutil.which(DEFAULT_RESTIC)
    if resolved:
        return resolved
    raise BackupError(f"restic binary not found (looked for {DEFAULT_RESTIC!r})")


def run(cmd: list[str], *, env: dict[str, str] | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if check and proc.returncode != 0:
        raise BackupError(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc


def resolve_backup_env() -> BackupEnv:
    fields = resolve_all_fields(BACKUP_ITEM_VAULT, BACKUP_ITEM_TITLE)
    missing = [k for k in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT", "R2_BUCKET") if not fields.get(k)]
    if missing:
        raise BackupError(
            f"missing required 1Password fields on {BACKUP_ITEM_TITLE!r}: {', '.join(missing)}"
        )
    repo = f"s3:{fields['R2_ENDPOINT'].rstrip('/')}/{fields['R2_BUCKET']}"
    # No separate password field is available and the item is read-only in this
    # environment, so derive the restic password from the existing R2 secret.
    # This keeps the restore secret in 1Password without needing write access.
    password = fields["R2_SECRET_ACCESS_KEY"]
    return BackupEnv(
        repo=repo,
        aws_access_key_id=fields["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=fields["R2_SECRET_ACCESS_KEY"],
        restic_password=password,
    )


def restic_env(cfg: BackupEnv) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "RESTIC_REPOSITORY": cfg.repo,
            "RESTIC_PASSWORD": cfg.restic_password,
            "AWS_ACCESS_KEY_ID": cfg.aws_access_key_id,
            "AWS_SECRET_ACCESS_KEY": cfg.aws_secret_access_key,
            "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION", "auto"),
            "AWS_REGION": env.get("AWS_REGION", "auto"),
            "AWS_S3_FORCE_PATH_STYLE": env.get("AWS_S3_FORCE_PATH_STYLE", "true"),
        }
    )
    return env


def repo_exists(env: dict[str, str]) -> bool:
    proc = run([restic_bin(), "snapshots", "--json"], env=env)
    if proc.returncode == 0:
        return True
    stderr = (proc.stderr or "").lower()
    stdout = (proc.stdout or "").lower()
    if "not initialized" in stderr or "not initialized" in stdout or "does not exist" in stderr:
        return False
    raise BackupError(f"restic snapshots failed unexpectedly (exit {proc.returncode})")


def ensure_repo(env: dict[str, str]) -> None:
    if repo_exists(env):
        return
    log("repository not initialized; running restic init")
    run([restic_bin(), "init"], env=env, check=True)


def backup_paths() -> list[str]:
    paths = []
    for p in BACKUP_TARGETS:
        if p.exists():
            paths.append(str(p))
        else:
            eprint(f"skipping missing path: {p}")
    return paths


def backup(env: dict[str, str], *, host: str) -> None:
    targets = backup_paths()
    if not targets:
        raise BackupError("no backup targets exist")
    cmd = [
        restic_bin(),
        "backup",
        "--host",
        host,
        "--tag",
        "hermes",
        "--tag",
        "offbox-nightly",
    ]
    for pattern in EXCLUDES:
        cmd.extend(["--exclude", pattern])
    cmd.extend(targets)
    log(f"running restic backup for {len(targets)} target(s)")
    run(cmd, env=env, check=True)
    log("running retention (keep-daily 7 / keep-weekly 4 / keep-monthly 6)")
    run(
        [
            restic_bin(),
            "forget",
            "--prune",
            f"--keep-daily={RETENTION['daily']}",
            f"--keep-weekly={RETENTION['weekly']}",
            f"--keep-monthly={RETENTION['monthly']}",
            "--host",
            host,
            "--tag",
            "offbox-nightly",
        ],
        env=env,
        check=True,
    )


def snapshots(env: dict[str, str]) -> list[dict[str, object]]:
    proc = run([restic_bin(), "snapshots", "--json"], env=env, check=True)
    if not proc.stdout.strip():
        return []
    data = json.loads(proc.stdout)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        snaps = data.get("snapshots")
        if isinstance(snaps, list):
            return snaps
    raise BackupError("unexpected restic snapshots JSON shape")


def latest_snapshot_id(env: dict[str, str], *, host: str, tag: str = "offbox-nightly") -> str:
    snaps = snapshots(env)
    filtered: list[dict[str, object]] = []
    for snap in snaps:
        hostname = snap.get("hostname")
        tags = snap.get("tags")
        tag_list = tags if isinstance(tags, list) else []
        if hostname == host and tag in tag_list:
            filtered.append(snap)
    if not filtered:
        raise BackupError(f"no snapshots found for host={host!r} tag={tag!r}")
    filtered.sort(key=lambda s: str(s.get("time", "")))
    snap_id = filtered[-1].get("short_id") or filtered[-1].get("id")
    if not snap_id:
        raise BackupError("snapshot id missing from restic output")
    return str(snap_id)


def restore_test(env: dict[str, str], *, host: str) -> None:
    src = HERMES_HOME / "config.yaml"
    if not src.exists():
        raise BackupError(f"source file missing: {src}")
    snap_id = latest_snapshot_id(env, host=host)
    scratch = Path(tempfile.mkdtemp(prefix="restic-restore-test-"))
    try:
        log(f"restoring config.yaml from snapshot {snap_id} into {scratch}")
        run(
            [
                restic_bin(),
                "restore",
                snap_id,
                "--target",
                str(scratch),
                "--include",
                str(src),
            ],
            env=env,
            check=True,
        )
        restored = list(scratch.rglob("config.yaml"))
        if not restored:
            raise BackupError(f"restored config.yaml not found under {scratch}")
        # Prefer the path that still contains .hermes in its ancestry.
        restored.sort(key=lambda p: (".hermes" not in str(p), len(str(p))))
        restored_file = restored[0]
        if not filecmp.cmp(src, restored_file, shallow=False):
            raise BackupError(
                f"restored config.yaml differs from source ({src} vs {restored_file})"
            )
        log(f"restore test passed: {restored_file} matches {src}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def resolve_slack_bot_token() -> str | None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        return token
    try:
        fields = resolve_all_fields("Dev Toolbox", "dev")
    except Exception as exc:  # noqa: BLE001 - resolution should fail open per alert path
        eprint(f"unable to resolve SLACK_BOT_TOKEN via 1Password SDK: {exc}")
        return None
    token = fields.get("SLACK_BOT_TOKEN")
    if not token:
        eprint("1Password item Dev Toolbox/dev has no SLACK_BOT_TOKEN field")
    return token


# --- Durable signature-based alert dedup (86e2abmmj) ------------------------
# Dedup on the distinct failure signature (not calendar day) so unchanged
# backup failures do not re-page every launchd run. State advances only after
# confirmed Slack delivery and survives process restarts on disk.
BACKUP_STATE_PATH = os.path.expanduser("~/.hermes/state/offbox-backup-monitor.json")


def _load_backup_state() -> dict:
    # Fail open: unreadable/corrupt state means no prior alert, so a real
    # failure remains eligible for delivery.
    try:
        with open(BACKUP_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _save_backup_state(state: dict) -> None:
    tmp = BACKUP_STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(BACKUP_STATE_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, BACKUP_STATE_PATH)


def _backup_failure_signature(host: str, exc: BaseException) -> str:
    return f"{host}:{type(exc).__name__}:{exc}"


def send_failure_alert(message: str) -> bool:
    """Send through Hermes and return whether delivery was confirmed."""
    if os.environ.get("HERMES_BACKUP_NO_ALERT"):
        eprint("alerts disabled via HERMES_BACKUP_NO_ALERT")
        return False
    hermes = Path(HERMES_BIN)
    if not hermes.exists():
        eprint(f"hermes binary missing; cannot send Slack alert: {hermes}")
        return False
    env = dict(os.environ)
    slack_token = resolve_slack_bot_token()
    if slack_token:
        env["SLACK_BOT_TOKEN"] = slack_token
    try:
        run([str(hermes), "send", "--to", SLACK_TARGET, message], env=env, check=True)
    except Exception as exc:  # noqa: BLE001 - alerting must not mask backup failure
        eprint(f"failed to send Slack alert: {exc}")
        return False
    return True


def maybe_send_failure_alert(host: str, exc: BaseException) -> bool:
    """Send a new failure once and persist only confirmed delivery."""
    sig = _backup_failure_signature(host, exc)
    state = _load_backup_state()
    if sig == state.get("last_alert_signature"):
        log(f"backup failure alert suppressed (unchanged signature, dedup state={BACKUP_STATE_PATH})")
        return True
    if not send_failure_alert(f"🚨 Hermes off-box backup failed on {host}: {exc}"):
        return False
    try:
        state["last_alert_signature"] = sig
        state["last_alert_at"] = time.time()
        _save_backup_state(state)
    except Exception as save_exc:  # noqa: BLE001 - alert already sent
        eprint(f"failed to persist backup-alert dedup state: {save_exc!r}")
        return False
    return True


def maybe_send_recovery_alert(host: str) -> bool:
    """Send one recovery and clear failure state after confirmed delivery."""
    state = _load_backup_state()
    if not state.get("last_alert_signature"):
        return True
    if not send_failure_alert(f"✅ Hermes off-box backup on {host} recovered."):
        return False
    try:
        state["last_alert_signature"] = None
        state["recovered_at"] = time.time()
        _save_backup_state(state)
    except Exception as save_exc:  # noqa: BLE001
        eprint(f"failed to persist backup-alert recovery state: {save_exc!r}")
        return False
    return True


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="backup", choices=["backup", "init", "check", "snapshots", "restore-test"])
    ap.add_argument("--host", default=DEFAULT_HOST)
    args = ap.parse_args(list(argv) if argv is not None else None)

    try:
        cfg = resolve_backup_env()
        env = restic_env(cfg)
        if args.mode == "init":
            ensure_repo(env)
            log("repository ready")
            return 0
        if args.mode == "check":
            ensure_repo(env)
            log("repository reachable")
            return 0
        if args.mode == "snapshots":
            ensure_repo(env)
            snaps = snapshots(env)
            print(json.dumps(snaps, indent=2))
            return 0
        if args.mode == "restore-test":
            ensure_repo(env)
            restore_test(env, host=args.host)
            return 0

        ensure_repo(env)
        backup(env, host=args.host)
        maybe_send_recovery_alert(args.host)
        return 0
    except Exception as exc:  # noqa: BLE001 - surface full failure detail
        eprint(str(exc))
        maybe_send_failure_alert(args.host, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
