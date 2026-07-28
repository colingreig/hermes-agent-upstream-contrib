#!/usr/bin/env python3
"""
verify-writer-chain.py
Conformance verifier for the Hermes Codex-OAuth code-writer chain.

Reads ~/.hermes/writer-chain.json as the declarative source of truth and
asserts each coupled point against the live system.

Checks:
  - flag: SDK-resolved 1Password HERMES_WRITER_CODEX value == "1" (fatal)
  - proxy: launchd + port 8646 live (fatal, auto-repair with --apply --restart)
  - opencode_jsonc: baseURL == http://127.0.0.1:8646/v1 (fatal, auto-repair with --apply)
  - oauth_token: JWT exp not past/near, active_provider == openai-codex
  - cascade_source: WRITER_CASCADE[0] matches manifest primary exactly

The 1Password check is intentionally non-interactive: it imports the adjacent
op_sdk_resolve.py service-account SDK resolver. It never invokes the op CLI and
never prints secret values.
"""

import argparse
import ast
import base64
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_PATH = Path.home() / ".hermes/writer-chain.json"
AUTH_PATH = Path.home() / ".hermes/auth.json"
JSONC_PATH = Path.home() / ".config/opencode/opencode.jsonc"
CANONICAL_JSONC = Path.home() / ".hermes/canonical/opencode.jsonc"
OC_EXEC_PATH = Path.home() / ".hermes/scripts/opencode_exec.py"
STATE_PATH = Path.home() / ".hermes/state/writer-chain-conformance.json"
HERMES_BIN = Path.home() / ".local/bin/hermes"

TOKEN_SKEW_SECONDS = 300
PROXY_PORT = 8646
PROXY_LABEL = "ai.hermes.codex-proxy"


def _ok(label, msg):
    print(f"  OK      [{label}] {msg}")


def _fail(label, msg):
    print(f"  FAIL    [{label}] {msg}")


def _repaired(label, msg):
    print(f"  REPAIRED[{label}] {msg}")


def _info(msg):
    print(f"          {msg}")


def _send_slack(msg):
    if os.environ.get("DRY_RUN"):
        print(f"[verify-writer-chain] DRY_RUN slack:\n{msg}")
        return
    try:
        subprocess.run(
            [str(HERMES_BIN), "send", "--to", "slack:hermes", msg],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        print(f"[verify-writer-chain] slack send failed: {exc!r}", file=sys.stderr)


def _load_op_sdk_resolver():
    resolver_path = Path(__file__).resolve().with_name("op_sdk_resolve.py")
    if not resolver_path.is_file():
        raise FileNotFoundError(f"SDK resolver missing at {resolver_path}")
    spec = importlib.util.spec_from_file_location("writer_chain_op_sdk_resolve", resolver_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load SDK resolver from {resolver_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolve_refs = getattr(module, "resolve_refs", None)
    if not callable(resolve_refs):
        raise AttributeError("SDK resolver does not expose resolve_refs")
    return resolve_refs


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as state_file:
            return json.load(state_file)
    except Exception:
        return {}


def _save_state(obj):
    tmp = str(STATE_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as state_file:
        json.dump(obj, state_file, indent=2)
    os.replace(tmp, str(STATE_PATH))


def check_flag(manifest):
    point = manifest["coupled_points"]["flag"]
    label = "flag"
    result = {"check": "flag", "status": "pass", "detail": ""}
    op_ref = point.get("source_of_truth", f"1password:op://Dev Toolbox/dev/{point['name']}")
    if op_ref.startswith("1password:"):
        op_ref = op_ref[len("1password:"):]
    try:
        resolve_refs = _load_op_sdk_resolver()
        value = resolve_refs([op_ref]).get(op_ref, "")
        if value == point["expected"]:
            _ok(label, f"1Password {point['name']} matches expected value (redacted; SDK resolver)")
        else:
            actual_desc = "missing" if not value else "different"
            _fail(label, f"1Password {point['name']} is {actual_desc} (expected value redacted) - writer Codex path DISABLED")
            result["status"] = "fail"
            result["detail"] = actual_desc
    except Exception as exc:
        _fail(label, f"exception checking 1Password via SDK resolver: {exc!r}")
        result["status"] = "fail"
        result["detail"] = str(exc)
    return result


def _proxy_loaded():
    try:
        run = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        return PROXY_LABEL in run.stdout
    except Exception:
        return False


def _port_listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def check_proxy(manifest, apply=False, restart=False):
    label = "proxy"
    result = {"check": "proxy", "status": "pass", "detail": "", "repaired": False}
    try:
        loaded = _proxy_loaded()
        port_up = _port_listening(PROXY_PORT)
        if loaded and port_up:
            _ok(label, f"{PROXY_LABEL} loaded + port {PROXY_PORT} listening")
            return result

        issues = []
        if not loaded:
            issues.append("launchd not loaded")
        if not port_up:
            issues.append(f"port {PROXY_PORT} not listening")
        _fail(label, f"{PROXY_LABEL}: {', '.join(issues)}")

        if apply and restart:
            uid = os.getuid()
            _info(f"AUTO-REPAIR: kickstarting {PROXY_LABEL}")
            if not loaded:
                plist = Path.home() / "Library/LaunchAgents/ai.hermes.codex-proxy.plist"
                if plist.exists():
                    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, timeout=10)
                    time.sleep(1)
                else:
                    _fail(label, f"plist not found at {plist} - cannot bootstrap")
                    result["status"] = "fail"
                    result["detail"] = "plist missing"
                    return result
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{PROXY_LABEL}"], capture_output=True, timeout=10)
            time.sleep(2)
            loaded2 = _proxy_loaded()
            port2 = _port_listening(PROXY_PORT)
            if loaded2 and port2:
                _repaired(label, f"{PROXY_LABEL} restarted + port {PROXY_PORT} now listening")
                result["repaired"] = True
            else:
                still = []
                if not loaded2:
                    still.append("still not in launchd")
                if not port2:
                    still.append(f"port {PROXY_PORT} still closed")
                _fail(label, f"auto-repair attempted but: {', '.join(still)}")
                result["status"] = "fail"
                result["detail"] = "; ".join(still)
        else:
            _info("  -> Re-run with --apply --restart to auto-repair")
            result["status"] = "fail"
            result["detail"] = "; ".join(issues)
    except Exception as exc:
        _fail(label, f"exception: {exc!r}")
        result["status"] = "fail"
        result["detail"] = str(exc)
    return result


def _parse_jsonc(path):
    text = Path(path).read_text(encoding="utf-8")
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i : i + 2] == "/*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
        elif text[i : i + 2] == "//":
            end = text.find("\n", i)
            if end == -1:
                break
            i = end
        elif text[i] == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                elif text[j] == '"':
                    j += 1
                    break
                else:
                    j += 1
            out.append(text[i:j])
            i = j
        else:
            out.append(text[i])
            i += 1
    return json.loads("".join(out))


def check_jsonc(manifest, apply=False):
    label = "opencode_jsonc"
    expected_url = "http://127.0.0.1:8646/v1"
    result = {"check": "opencode_jsonc", "status": "pass", "detail": "", "repaired": False}
    try:
        if not JSONC_PATH.exists():
            _fail(label, f"{JSONC_PATH} does not exist")
            result["status"] = "fail"
            result["detail"] = "file missing"
        else:
            try:
                cfg = _parse_jsonc(JSONC_PATH)
                actual_url = cfg.get("provider", {}).get("openai", {}).get("options", {}).get("baseURL", "")
                if actual_url == expected_url:
                    _ok(label, f"opencode.jsonc openai.options.baseURL={actual_url!r}")
                    return result
                _fail(label, f"opencode.jsonc openai.options.baseURL={actual_url!r} (expected {expected_url!r})")
                result["status"] = "fail"
                result["detail"] = f"actual={actual_url!r}"
            except Exception as exc:
                _fail(label, f"parse error in {JSONC_PATH}: {exc!r}")
                result["status"] = "fail"
                result["detail"] = f"parse error: {exc!r}"

        if result["status"] == "fail" and apply:
            if not CANONICAL_JSONC.exists():
                _fail(label, f"canonical copy missing at {CANONICAL_JSONC} - cannot auto-repair")
                result["detail"] += "; canonical missing"
            else:
                _info(f"AUTO-REPAIR: overwriting {JSONC_PATH} from {CANONICAL_JSONC}")
                import shutil

                shutil.copy2(str(CANONICAL_JSONC), str(JSONC_PATH))
                cfg2 = _parse_jsonc(JSONC_PATH)
                actual2 = cfg2.get("provider", {}).get("openai", {}).get("options", {}).get("baseURL", "")
                if actual2 == expected_url:
                    _repaired(label, f"opencode.jsonc restored from canonical; baseURL={actual2!r}")
                    result["status"] = "pass"
                    result["repaired"] = True
                else:
                    _fail(label, f"after repair, baseURL={actual2!r} - canonical may itself be wrong")
                    result["detail"] += f"; post-repair url={actual2!r}"
        elif result["status"] == "fail":
            _info("  -> Re-run with --apply to auto-repair from canonical copy")
    except Exception as exc:
        _fail(label, f"exception: {exc!r}")
        result["status"] = "fail"
        result["detail"] = str(exc)
    return result


def check_oauth_token(manifest):
    label = "oauth_token"
    point = manifest["coupled_points"]["oauth_token"]
    result = {"check": "oauth_token", "status": "pass", "detail": "", "repaired": False}
    try:
        if not AUTH_PATH.exists():
            _fail(label, f"{AUTH_PATH} does not exist - no provider configured")
            result["status"] = "fail"
            result["detail"] = "auth.json missing"
            return result

        with open(AUTH_PATH, encoding="utf-8") as auth_file:
            auth = json.load(auth_file)

        active = auth.get("active_provider", "")
        expected_provider = point["assert_active_provider"]
        if active != expected_provider:
            _fail(label, f"active_provider={active!r} (expected {expected_provider!r})")
            _info("  -> HARD-REFUSE: set active_provider manually or re-run codex CLI auth")
            result["status"] = "fail"
            result["detail"] = f"active_provider={active!r}"
            return result

        obj = auth
        for key in point["token_location"].split("."):
            obj = obj.get(key, {})
        access_token = obj if isinstance(obj, str) else ""
        if not access_token:
            _fail(label, "access_token is empty or missing")
            _info("  -> HARD-REFUSE: re-authenticate via codex CLI")
            result["status"] = "fail"
            result["detail"] = "access_token missing"
            return result

        parts = access_token.split(".")
        if len(parts) < 2:
            _fail(label, "access_token is not a valid JWT (wrong number of segments)")
            result["status"] = "fail"
            result["detail"] = "not a JWT"
            return result

        segment = parts[1]
        pad = segment + "=" * ((4 - len(segment) % 4) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(pad))
        except Exception as exc:
            _fail(label, f"JWT payload decode error: {exc!r}")
            result["status"] = "fail"
            result["detail"] = f"JWT decode error: {exc!r}"
            return result

        remaining = payload.get("exp", 0) - time.time()
        if remaining <= 0:
            _fail(label, f"OAuth access_token EXPIRED (exp was {int(-remaining)}s ago)")
            _info("  -> HARD-REFUSE: refresh via codex CLI (Colin must re-auth)")
            result["status"] = "fail"
            result["detail"] = "token expired"
        elif remaining <= TOKEN_SKEW_SECONDS:
            _fail(label, f"OAuth access_token expires in {int(remaining)}s (<{TOKEN_SKEW_SECONDS}s skew window)")
            _info("  -> HARD-REFUSE: refresh via codex CLI before next writer run")
            result["status"] = "fail"
            result["detail"] = f"near-expiry: {int(remaining)}s remaining"
        else:
            hours = remaining / 3600
            _ok(label, f"active_provider=openai-codex, access_token valid ({hours:.1f}h remaining)")
            result["detail"] = f"expires_in={int(remaining)}s"
    except Exception as exc:
        _fail(label, f"exception: {exc!r}")
        result["status"] = "fail"
        result["detail"] = str(exc)
    return result


def _writer_cascade_first(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "WRITER_CASCADE" for target in node.targets):
            continue
        if not isinstance(node.value, ast.List) or not node.value.elts:
            raise ValueError("WRITER_CASCADE is not a non-empty list literal")
        first = node.value.elts[0]
        if not isinstance(first, ast.Tuple) or len(first.elts) != 2:
            raise ValueError("WRITER_CASCADE[0] is not a 2-tuple literal")
        model = ast.literal_eval(first.elts[0])
        provider = ast.literal_eval(first.elts[1])
        return model, provider
    raise ValueError("WRITER_CASCADE assignment not found")


def check_cascade_source(manifest):
    label = "cascade_source"
    point = manifest["cascade_source"]
    expected_model = manifest["primary"]["model"]
    expected_provider = manifest["primary"]["provider"]
    result = {"check": "cascade_source", "status": "pass", "detail": ""}
    try:
        path = Path(os.path.expanduser(point.get("path") or str(OC_EXEC_PATH)))
        if not path.exists():
            _fail(label, f"{path} not found - cannot cross-check WRITER_CASCADE")
            result["status"] = "fail"
            result["detail"] = "file missing"
            return result

        actual = _writer_cascade_first(path)
        expected = (expected_model, expected_provider)
        if actual == expected:
            _ok(label, f"WRITER_CASCADE[0]==({expected_model!r}, {expected_provider!r}) matches manifest primary")
        else:
            _fail(label, f"WRITER_CASCADE[0]={actual!r} (expected {expected!r})")
            _info("  -> manifest and live opencode_exec.py have diverged; update one to match the other")
            result["status"] = "fail"
            result["detail"] = "cascade[0] mismatch"
    except Exception as exc:
        _fail(label, f"exception: {exc!r}")
        result["status"] = "fail"
        result["detail"] = str(exc)
    return result


def _maybe_alert(checks, args):
    if not args.alert:
        return
    overall_pass = all(check["status"] == "pass" for check in checks)
    prev_pass = _load_state().get("overall", "unknown") == "pass"
    if not overall_pass and prev_pass:
        failed_names = [check["check"] for check in checks if check["status"] == "fail"]
        repaired = [check["check"] for check in checks if check.get("repaired")]
        msg_parts = [
            f":rotating_light: *Hermes writer-chain degraded* - {len(failed_names)} check(s) FAILED:",
            *[f"  - `{name}`" for name in failed_names],
        ]
        if repaired:
            msg_parts.append(f":wrench: Auto-repaired: {', '.join(repaired)}")
        still_failed = [name for name in failed_names if name not in repaired]
        if still_failed:
            msg_parts.append(f":x: Still failed (needs human): {', '.join(still_failed)}")
        msg_parts.append("_(verify-writer-chain.py)_")
        _send_slack("\n".join(msg_parts))
    elif overall_pass and not prev_pass:
        _send_slack(":white_check_mark: *Hermes writer-chain RECOVERED* - all checks pass _(verify-writer-chain.py)_")


def main():
    parser = argparse.ArgumentParser(description="Hermes writer-chain conformance verifier")
    parser.add_argument("--apply", action="store_true", help="perform auto-repairs")
    parser.add_argument("--restart", action="store_true", help="allow launchctl actions (requires --apply)")
    parser.add_argument("--json", action="store_true", help="emit structured JSON result to stdout")
    parser.add_argument("--alert", action="store_true", help="send Slack on healthy->degraded transition")
    args = parser.parse_args()

    try:
        with open(MANIFEST_PATH, encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except Exception as exc:
        print(f"FAIL [manifest] Cannot load {MANIFEST_PATH}: {exc!r}", file=sys.stderr)
        return 1

    mode_desc = "REPORT-ONLY"
    if args.apply and args.restart:
        mode_desc = "APPLY+RESTART"
    elif args.apply:
        mode_desc = "APPLY (no restart)"

    print(f"\n== Hermes writer-chain conformance ({mode_desc}) ==")
    print(f"   manifest: {MANIFEST_PATH}")
    print(f"   primary:  {manifest['primary']['model']} via {manifest['primary']['provider']}")
    print(f"   failover: {manifest['failover']['model']} via {manifest['failover']['provider']}")
    print()

    checks = []
    for fn, kwargs in [
        (check_flag, {"manifest": manifest}),
        (check_proxy, {"manifest": manifest, "apply": args.apply, "restart": args.restart}),
        (check_jsonc, {"manifest": manifest, "apply": args.apply}),
        (check_oauth_token, {"manifest": manifest}),
        (check_cascade_source, {"manifest": manifest}),
    ]:
        try:
            result = fn(**kwargs)
        except Exception as exc:
            name = fn.__name__.replace("check_", "")
            _fail(name, f"unexpected exception in check: {exc!r}")
            result = {"check": name, "status": "fail", "detail": f"unexpected: {exc!r}"}
        checks.append(result)

    passed = [check for check in checks if check["status"] == "pass"]
    failed = [check for check in checks if check["status"] == "fail"]
    repaired = [check for check in checks if check.get("repaired")]
    overall = "pass" if not failed else "fail"

    print()
    print(f"== Result: {overall.upper()} ==")
    print(f"   passed={len(passed)}, failed={len(failed)}, repaired={len(repaired)}")
    if failed:
        print(f"   FAILED checks: {[check['check'] for check in failed]}")

    state_obj = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "mode": mode_desc,
        "checks": [{k: v for k, v in check.items() if k != "token"} for check in checks],
    }
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _save_state(state_obj)
    except Exception as exc:
        print(f"  WARN: could not write state file: {exc!r}", file=sys.stderr)

    try:
        _maybe_alert(checks, args)
    except Exception as exc:
        print(f"  WARN: alert failed: {exc!r}", file=sys.stderr)

    if args.json:
        print(json.dumps(state_obj, indent=2))

    return 0 if overall == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
