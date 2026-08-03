#!/usr/bin/env python3
"""Supabase security-advisor watcher — cheap, deterministic, zero-LLM.

Closes the days-long detection gap where a public table with RLS disabled (or any
ERROR-level Supabase security lint) sits exposed until Supabase's emailed notice
arrives. Sweeps EVERY Supabase project the management token can see, runs the
security advisor on each, and alerts to slack:hermes ONLY on CHANGE vs the last
poll (newly-exposed or recovered) so it never spams. $0 + sub-second per project
when idle (management API only). Never raises out of main().

Why this exists: jdmbuysell prod leaked 9 anon-readable `_backup_*` tables (incl.
buyer-PII `_leads` copies) 2026-06-10..06-13 created by a dealer merge via
`CREATE TABLE public._backup_... AS SELECT` (no inherited RLS). Supabase's email
lagged the fix by 3+ days. This is the 3rd occurrence of that class on that project.
See brain: learnings/2026-06-16-supabase-rls-disabled-public-backup-tables.

What it flags (per project):
  - rls_disabled_in_public   (the exposure — public table reachable by anon)
  - any lint with level == "ERROR"   (critical-class: exposed_auth_users,
    security_definer_view, etc.)
WARN/INFO lints (rls_enabled_no_policy = safe deny-all, search_path, sd-func
execute grants) are hardening backlog, not live exposure — intentionally ignored.

State: ~/.hermes/scripts/.supabase_rls_state.json   Alerts via `hermes send`.
Token: $SUPABASE_ACCESS_TOKEN (1Password-injected by the gateway via op-env), with
an `op read` fallback (Doppler decommissioned 2026-07-03). Wire as a no-agent cron.
"""
import json, os, subprocess, sys, urllib.request, urllib.error
from datetime import datetime, timezone

STATE_PATH = os.path.expanduser("~/.hermes/scripts/.supabase_rls_state.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
API = "https://api.supabase.com"
HTTP_TIMEOUT = 30
# Lint names that always count as live exposure regardless of advisor "level".
CRITICAL_NAMES = {"rls_disabled_in_public", "exposed_auth_users", "policy_exists_rls_disabled"}
PROJECT_STATUSES = {
    "INACTIVE", "ACTIVE_HEALTHY", "ACTIVE_UNHEALTHY", "COMING_UP", "UNKNOWN",
    "GOING_DOWN", "INIT_FAILED", "REMOVED", "RESTORING", "UPGRADING",
    "PAUSING", "RESTORE_FAILED", "RESTARTING", "PAUSE_FAILED", "RESIZING",
}


class ProbeError(RuntimeError):
    """A durable reason that the security state could not be established."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _token(*, env_only=False):
    tok = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if tok:
        return tok.strip()
    if env_only:
        return None
    # Fallback if the env var wasn't injected (e.g. manual run outside the gateway).
    # Migrated off `op read` 2026-07-06 (86e2625xg) — standing directive is to never
    # shell out to the `op` CLI (op read/op run hang under OP_SERVICE_ACCOUNT_TOKEN,
    # the same hang class that took the gateway down for hours on 2026-07-05); use
    # the 1Password service-account SDK via op_sdk_resolve.py instead, same pattern
    # as opencode_exec.py's cascade-flag self-heal.
    try:
        import importlib.util as _ilu
        _osr_path = os.path.join(os.path.dirname(__file__), "op_sdk_resolve.py")
        _osr_spec = _ilu.spec_from_file_location("op_sdk_resolve", _osr_path)
        _osr = _ilu.module_from_spec(_osr_spec)
        _osr_spec.loader.exec_module(_osr)
        ref = "op://Dev Toolbox/dev/SUPABASE_ACCESS_TOKEN"
        values = _osr.resolve_refs([ref])
        if ref in values and values[ref]:
            return values[ref].strip()
    except Exception as e:
        print(f"[supabase-rls] op_sdk_resolve fallback failed: {e!r}", file=sys.stderr)
    return None


def _api_get(path, token):
    """GET {API}{path} -> parsed JSON; fail closed on transport or JSON errors."""
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 # Cloudflare in front of api.supabase.com 403s the default
                 # Python-urllib UA; send a normal one.
                 "User-Agent": "hermes-supabase-rls-guard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "null")
    except Exception as e:
        raise ProbeError(f"GET {path} failed: {e!r}") from e


def _projects(token):
    data = _api_get("/v1/projects", token)
    if not isinstance(data, list):
        raise ProbeError("malformed projects response: expected a list")
    for project in data:
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            raise ProbeError("malformed projects response: project is missing a string id")
        status = project.get("status")
        if not isinstance(status, str) or status not in PROJECT_STATUSES:
            raise ProbeError("malformed projects response: project has invalid status")
    # Only sweep live projects; paused/inactive ones can't leak via the Data API.
    return [project for project in data if project["status"] == "ACTIVE_HEALTHY"]


def _exposures(ref, token):
    """Return {key: detail} for live-exposure lints in one project."""
    data = _api_get(f"/v1/projects/{ref}/advisors/security", token)
    lints = data.get("lints") if isinstance(data, dict) else data
    if not isinstance(lints, list):
        raise ProbeError(f"malformed advisor response for project {ref}: expected lints list")
    out = {}
    for l in lints:
        if not isinstance(l, dict):
            raise ProbeError(f"malformed advisor response for project {ref}: lint is not an object")
        name = l.get("name", "")
        is_exposure = name in CRITICAL_NAMES or l.get("level") == "ERROR"
        if not is_exposure:
            continue
        metadata = l.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ProbeError(f"malformed advisor response for project {ref}: metadata is not an object")
        obj = metadata.get("name") or l.get("cache_key") or name
        out[f"{ref}::{name}::{obj}"] = {
            "level": l.get("level"),
            "title": l.get("title", name),
            "detail": (l.get("detail") or "")[:160],
        }
    return out


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _send_slack(msg):
    if os.environ.get("DRY_RUN"):
        print(f"[supabase-rls] DRY_RUN slack:\n{msg}")
        return
    try:
        subprocess.run([HERMES_BIN, "send", "--to", "slack:hermes", msg],
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"[supabase-rls] slack send failed: {e!r}", file=sys.stderr)


def main():
    probe = "--probe" in sys.argv[1:]
    token = _token(env_only=probe)
    if not token:
        message = "[supabase-rls] no SUPABASE_ACCESS_TOKEN — cannot run"
        print(message, file=sys.stderr)
        if probe:
            print(json.dumps({"status": "error", "reason": "SUPABASE_ACCESS_TOKEN unavailable"}, sort_keys=True))
        return 2

    try:
        projects = _projects(token)
    except Exception as exc:
        reason = f"projects request failed: {exc}"
        if probe:
            print(json.dumps({"status": "error", "reason": reason}, sort_keys=True))
        else:
            print(f"[supabase-rls] {reason}", file=sys.stderr)
        return 2
    names = {p["id"]: p.get("name", p["id"]) for p in projects}

    current = {}
    errors = []
    for p in projects:
        try:
            current.update(_exposures(p["id"], token))
        except Exception as exc:
            errors.append({"project": p["id"], "reason": str(exc)})

    if errors:
        result = {"status": "error", "projects": len(projects), "errors": errors}
        if probe:
            print(json.dumps(result, sort_keys=True))
        else:
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 2

    if probe:
        print(json.dumps({"status": "alert" if current else "clean",
                          "projects": len(projects), "exposed": len(current),
                          "exposures": current}, sort_keys=True))
        return 1 if current else 0

    prev = _load_state().get("exposures", {})
    newly = [k for k in current if k not in prev]
    recovered = [k for k in prev if k not in current]

    lines = []
    if newly:
        lines.append(f"🔴 *Supabase exposure detected* ({len(newly)}):")
        for k in sorted(newly):
            ref, lint, obj = k.split("::", 2)
            i = current[k]
            lines.append(f"  • `{names.get(ref, ref)}` [{i['level']}] {lint} → `{obj}`")
            if i["detail"]:
                lines.append(f"      {i['detail']}")
    if recovered:
        lines.append(f"🟢 *Supabase exposure cleared* ({len(recovered)}): " +
                     ", ".join(f"`{names.get(k.split('::',1)[0], k.split('::',1)[0])}` / {k.split('::',2)[1]}"
                               for k in sorted(recovered)))
    if lines:
        lines.append(f"_(still exposed: {len(current)} across "
                     f"{len({k.split('::',1)[0] for k in current})} project(s) — `supabase_rls_guard.py`)_")
        _send_slack("\n".join(lines))

    _save_state({"generated_at": _now_iso(), "exposures": current})
    # STDOUT stays EMPTY (no-agent silent-watchdog contract); summary to STDERR.
    print(json.dumps({"projects": len(projects), "exposed": len(current),
                      "newly": len(newly), "recovered": len(recovered)}),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[supabase-rls] unexpected: {e!r}", file=sys.stderr)
        if "--probe" in sys.argv[1:]:
            print(json.dumps({"status": "error", "reason": f"unexpected: {e!r}"}, sort_keys=True))
        sys.exit(2)
