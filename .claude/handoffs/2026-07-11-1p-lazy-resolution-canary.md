# Handoff — 1Password lazy-resolution: live canary + cutover (ClickUp 86e29q8je / 86e29ru93)

Expires: stale after 2026-07-25 (re-verify all facts against live state before acting).

## Status
- Core machinery MERGED to fork `main`: PR #27, squash `f1008ee` (colingreig/hermes-agent). Flag `HERMES_LAZY_SECRET_RESOLUTION` default OFF = inert.
- Design doc merged earlier: PR #25 (`2199279`), `docs/design/per-task-1p-secret-resolution.md`.
- **PROD WAS NOT TOUCHED.** Recon was read-only; the live canary was paused before execution and handed off for testing.

## CRITICAL — deploy first
The resolver is NOT on the running gateway yet. `~/.hermes/hermes-agent` is on `prod-live-patches` (@ `aac364615c`); `agent/lazy_secret_resolver.py` is absent, `f1008ee` not present. You MUST deploy before any canary:
1. On the mini: `cd ~/.hermes/hermes-agent`, fetch `fork`, merge `fork/main` into `prod-live-patches` (the standard deploy — NEVER `pull origin`; origin = NousResearch upstream). `f1008ee` is on `fork/main`.
2. ~9 local hand-patches are applied as uncommitted working-tree edits, reapplied by `~/.hermes/scripts/verify-hermes-patches.sh` (uses `git apply --3way`, stops loud on conflict). After the merge run `verify-hermes-patches.sh --apply` to reconcile — resolve conflicts loudly, NEVER `git reset --hard`.
3. Confirm `agent/lazy_secret_resolver.py` exists and `~/.hermes/hermes-agent/venv/bin/python -c "import agent.lazy_secret_resolver"` succeeds.

## Canary — ZAI_API_KEY only
ZAI_API_KEY (base) is the cleanest var that exercises the real lazy path (auth.py::_resolve_api_key_provider_secret -> get_env_value_prefer_dotenv -> lazy tier). Steps:
4. Backup then edit `~/.hermes/scripts/gateway_secrets_wrap.sh` (OUTSIDE the repo — safe to hand-edit, untouched by `hermes update`). After its `set -a; . "$resolved_env"; set +a` source line, add:
   `[ "$HERMES_LAZY_SECRET_RESOLUTION" = "1" ] && unset ZAI_API_KEY`
   (`cp gateway_secrets_wrap.sh gateway_secrets_wrap.sh.bak.$(date +%Y%m%dT%H%M%S)` first.)
5. Set the flag in the gateway plist EnvironmentVariables: add `HERMES_LAZY_SECRET_RESOLUTION=1` to `~/Library/LaunchAgents/ai.hermes.gateway.plist` (PlistBuddy/plutil). Back up the plist first.
6. Restart: `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`. DO NOT use `hermes gateway restart|install` (regenerates the plist, wipes the secrets wrapping) or `~/.hermes/rewrap-gateway.sh` (dead Doppler script, decommissioned 2026-07-03).
7. Gateway may not be idle — check `ps aux | grep "hermes cron run" | grep -v grep`; prefer to wait for active cron runs, or accept transient terminal-timeout warnings.

## Verify
8. Health: `~/.hermes/gateway_state.json` shows a NEW `pid` + `gateway_state:"running"` + fresh `updated_at`; `tail ~/.hermes/logs/gateway.error.log` shows "1Password SDK resolve succeeded", no FATAL / crash-loop (KeepAlive relaunches on failure — watch for a flapping pid).
9. Lazy path serves zai (prints only length, never the value):
   `~/.hermes/hermes-agent/venv/bin/python -c "import os; os.environ.pop('ZAI_API_KEY',None); os.environ['HERMES_LAZY_SECRET_RESOLUTION']='1'; from hermes_cli.config import get_env_value_prefer_dotenv as g; v=g('ZAI_API_KEY'); print('LAZY-RESOLVED' if v and len(v)>10 else 'FAIL','len=',len(v or ''))"`
   Expect LAZY-RESOLVED.
10. os.environ clean: run `scripts/verify_gateway_secret_env.py` in a freshly-wrapped gateway shell — ZAI_API_KEY absent from the boot-resident set.
11. Confirm a real zai/glm-4.7 op still works (validator chains use zai) and credential_pool is healthy.

## Rollback (instant, ~1 min)
Unset `HERMES_LAZY_SECRET_RESOLUTION` in the plist (or set 0), restore `gateway_secrets_wrap.sh` from the `.bak`, `launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway`. Boot re-exports ZAI_API_KEY (legacy path).

## After the canary — broader cutover (86e29ru93)
Needs two more code pieces before more vars can leave boot:
- Wire the flag-gated lazy tier into `agent/secret_scope.py::get_secret` (the 3rd chokepoint; MCP `${VAR}` interpolation bottoms out there for DATAFORSEO_LOGIN/PASSWORD, MCP_AGENCY_OS_API_KEY, WORKBENCH_MCP_TOKEN).
- Give the no-agent cron scripts their own resolution (mirror opencode_exec.py::_op_secret) or on-demand injection in cron/scheduler.py::_run_job_script: postmark_inbound_gate.py, clickup_poll_gate.py, staleness_sweep.py, clickup_groomer.py, and plugins/platforms/slack/adapter.py (CLICKUP_API_TOKEN in-process).
SAFE to drop from boot now: CRON_SECRET (no consumer), ZAI_API_KEY_HERMES + ANTHROPIC_API_KEY_HERMES (self-resolve / dead path). UNSAFE until the two pieces land: DataForSEO*, the MCP tokens, POSTMARK_*, CLICKUP_API_TOKEN.

## Recon facts
- Gateway: launchd label `ai.hermes.gateway`, plist `~/Library/LaunchAgents/ai.hermes.gateway.plist`, was PID 18684, KeepAlive=true. Bind 127.0.0.1:8642. venv python 3.11.15.
- Launch chain: plist -> `gateway_secrets_wrap.sh` (1Password SDK via op_sdk_resolve.py, 30s timeout x3, refuses boot on unresolved) -> `gateway_launch_inner.sh` -> `venv/bin/python -m hermes_cli.main gateway run --replace`.
- `op-secrets.env`: 43 `op://` entries; ZAI_API_KEY line 51, ZAI_API_KEY_HERMES line 52.
- Full secret-coupling map: memory `hermes-gateway-secret-env-coupling`.
