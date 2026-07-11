# Handoff — 1Password lazy-resolution: live canary + cutover (ClickUp 86e29q8je / 86e29ru93)

Expires: stale after 2026-07-25 (re-verify all facts against live state before acting).

## Status
- Core machinery MERGED to fork `main`: PR #27, squash `f1008ee` (colingreig/hermes-agent). Flag `HERMES_LAZY_SECRET_RESOLUTION` gates it.
- Design doc merged earlier: PR #25 (`2199279`), `docs/design/per-task-1p-secret-resolution.md`.
- **CANARY EXECUTED + VERIFIED LIVE 2026-07-11.** fork/main merged into `prod-live-patches` on the mini (HEAD `e33b6208`, `f1008eec56` present); gateway restarted with `HERMES_LAZY_SECRET_RESOLUTION=1`. Verified live: `ZAI_API_KEY` ABSENT from the running gateway process's os.environ, lazy path resolves it (len 49), key authenticates (HTTP 200 vs z.ai), zai credential_pool healthy, gateway stable (Slack+API connected). ZAI is the first secret to leave boot. **The flag is LIVE on prod — do NOT assume OFF.**
- Two runbook bugs were found + fixed while running the canary (corrected in the steps below): the wrapper line needed a `set -u` guard, and `launchctl kickstart -k` does NOT reload plist `EnvironmentVariables` (must bootout+bootstrap).

## CRITICAL — deploy first
The resolver is NOT on the running gateway yet. `~/.hermes/hermes-agent` is on `prod-live-patches` (@ `aac364615c`); `agent/lazy_secret_resolver.py` is absent, `f1008ee` not present. You MUST deploy before any canary:
1. On the mini: `cd ~/.hermes/hermes-agent`, fetch `fork`, merge `fork/main` into `prod-live-patches` (the standard deploy — NEVER `pull origin`; origin = NousResearch upstream). `f1008ee` is on `fork/main`.
2. NOTE (2026-07-11): the prod hand-patches are now COMMITTED into `prod-live-patches` (the "uncommitted working-tree edits" framing is stale), so the tree is clean and the merge is straightforward (only `hermes_cli/config.py` touches both sides; it auto-merges). `~/.hermes/scripts/verify-hermes-patches.sh --apply` still runs a residual patch set and **deterministically re-breaks Slack** (stale patch 05 leaves `<<<<<<<` conflict markers → `UU` in `plugins/platforms/slack/adapter.py`). If adapter.py ends up conflicted, restore it with `git checkout HEAD -- plugins/platforms/slack/adapter.py` (the committed version is the known-good one the running gateway uses). NEVER `git reset --hard`.
3. Confirm `agent/lazy_secret_resolver.py` exists and `~/.hermes/hermes-agent/venv/bin/python -c "import agent.lazy_secret_resolver"` succeeds.

## Canary — ZAI_API_KEY only
ZAI_API_KEY (base) is the cleanest var that exercises the real lazy path (auth.py::_resolve_api_key_provider_secret -> get_env_value_prefer_dotenv -> lazy tier). Steps:
4. Backup then edit `~/.hermes/scripts/gateway_secrets_wrap.sh` (OUTSIDE the repo — safe to hand-edit, untouched by `hermes update`). After its `set -a; . "$resolved_env"; set +a` source line, add:
   `[ "${HERMES_LAZY_SECRET_RESOLUTION:-}" = "1" ] && unset ZAI_API_KEY`  — the `:-` guard is REQUIRED: `gateway_secrets_wrap.sh` runs under `set -u`, so the unguarded `"$HERMES_LAZY_SECRET_RESOLUTION"` throws "unbound variable" → non-zero exit → KeepAlive crash-loops the gateway (learned the hard way 2026-07-11).
   (`cp gateway_secrets_wrap.sh gateway_secrets_wrap.sh.bak.$(date +%Y%m%dT%H%M%S)` first.)
5. Set the flag in the gateway plist EnvironmentVariables: add `HERMES_LAZY_SECRET_RESOLUTION=1` to `~/Library/LaunchAgents/ai.hermes.gateway.plist` (PlistBuddy/plutil). Back up the plist first.
6. Restart via **bootout + bootstrap** (NOT `kickstart -k` — kickstart restarts the *cached* launchd job and does NOT pick up the plist `EnvironmentVariables` you just added, so the flag never reaches the process): `launchctl bootout gui/$(id -u)/ai.hermes.gateway; sleep 5; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway.plist`. If bootstrap returns `5: Input/output error` (race with the old instance tearing down), wait ~5s and retry once. DO NOT use `hermes gateway restart|install` (regenerates the plist, wipes the secrets wrapping) or `~/.hermes/rewrap-gateway.sh` (dead Doppler script, decommissioned 2026-07-03).
7. Gateway may not be idle — check `ps aux | grep "hermes cron run" | grep -v grep`; prefer to wait for active cron runs, or accept transient terminal-timeout warnings.

## Verify
8. Health: `~/.hermes/gateway_state.json` shows a NEW `pid` + `gateway_state:"running"` + fresh `updated_at`; `tail ~/.hermes/logs/gateway.error.log` shows "1Password SDK resolve succeeded", no FATAL / crash-loop (KeepAlive relaunches on failure — watch for a flapping pid).
9. Lazy path serves zai (prints only length, never the value):
   `~/.hermes/hermes-agent/venv/bin/python -c "import os; os.environ.pop('ZAI_API_KEY',None); os.environ['HERMES_LAZY_SECRET_RESOLUTION']='1'; from hermes_cli.config import get_env_value_prefer_dotenv as g; v=g('ZAI_API_KEY'); print('LAZY-RESOLVED' if v and len(v)>10 else 'FAIL','len=',len(v or ''))"`
   Expect LAZY-RESOLVED.
10. os.environ clean: run `scripts/verify_gateway_secret_env.py` in a freshly-wrapped gateway shell — ZAI_API_KEY absent from the boot-resident set.
11. Confirm a real zai/glm-4.7 op still works (validator chains use zai) and credential_pool is healthy.

## Rollback (instant, ~1 min)
Restore `gateway_secrets_wrap.sh` from its `.bak` (removes the crashing unset line) and restore the plist from its `.bak` (drops the flag), then reload with **bootout + bootstrap** (as in step 6 — `kickstart -k` won't drop the flag from the cached job). Boot re-exports ZAI_API_KEY (legacy path). Restoring just the wrapper `.bak` + any relaunch is enough to stop a crash-loop, since the wrapper is re-read from disk each boot.

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
