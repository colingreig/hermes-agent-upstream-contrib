# Hermes Rebuild Cutover — paused 2026-07-29 (mini power outage)

Resume this runbook when the Mac mini has power. Session context: Colin ordered a full gut-and-rebuild of the Hermes fleet on 2026-07-29. Code side is DONE (PR #210, merge sha ef76057787). Only the mini-side cutover remains.

## State when paused
- **PR #210 merged to main**: this `machine-setup/fleet-config/` bundle — 5 profiles, 15-job jobs.json, config-overlay.yaml, sha256-pinned `install_fleet_config.py` (+ tests), README. The later fleet executor correction restores direct ClickUp execution; rollback scope remains test-proven.
- **Mini**: all 36 old crons frozen (`enabled=false`); full snapshot at `~/hermes-archive-20260729/` (hermes-state.tgz = config.yaml, cron, scripts, LaunchAgents, opencode.jsonc; plus jobs.json.pre-freeze). The OLD gateway was still running when power died — after power restore, auto-login will relaunch it via LaunchAgent. Shut it down before anything else.
- **ClickUp** (list 901714465284): board cleared (108 old tasks closed); 10 REBUILD tasks tagged `rebuild-0729` (86e2jbbey…86e2jbbtv); 3 preserved non-hermes tasks.
- **Fallback chain**: openai-codex/gpt-5.5 → zai/glm-4.7 → nous/deepseek/deepseek-v4-pro (effort `high`, `inference-api.nousresearch.com/v1`). `NOUS_PORTAL_API_KEY` lives in 1Password at `op://hermes-agent/nous_portal_api_key/password`; wire into `~/.hermes/scripts/op-secrets.env` on the mini for lazy resolution.
- **Hard rules**: codex-oauth is the only OpenAI surface (billed openai-api banned everywhere); content = anthropic/claude-sonnet-5 fail-closed, NO fallback (verified: profile sessions do not inherit root fallback_providers); ClickUp is the sole board; its fleet executors run the direct skill paths and move tasks to In Review, never Complete (ignite-validate owns Complete). Upstream Hermes kanban remains available but is not part of the fleet ClickUp path.
- **Production release path**: the rebuilt fleet uses manual governed release
  cuts only. `com.colingreig.hermes.release-poll` is retired for this fleet and
  is not installed, loaded, or enabled. Every production cut must name the
  exact certified full SHA as both `--ref` and `--certified-sha`, plus the
  immutable promotion receipt ID via `--promotion-receipt-id`. Generic poller
  tooling remains in the repo only as an explicitly adopted contingency for
  other deployments.
- **Shared Chrome CDP**: `com.colingreig.chrome-cdp` owns a visible Chrome
  instance with profile `~/.hermes/chrome-cdp-profile`. Chrome listens only on
  `127.0.0.1:9222`; Tailscale Serve forwards the mini's tailnet-only
  `100.84.74.65:9222` endpoint to that loopback listener. Persistent Hermes
  config uses `browser.cdp_url: http://127.0.0.1:9222` (the config equivalent
  of `BROWSER_CDP_URL`). Canonical assets are
  `machine-setup/mini-scripts/chrome_cdp_launch.sh` and
  `machine-setup/mini-scripts/launchd/com.colingreig.chrome-cdp.plist`.

## Runbook
1. **Preflight**: `ssh mini` reachable; snapshot dir exists; check gateway state (`launchctl list | grep -i hermes`; it will have auto-relaunched).
2. **Shut down old gateway**: `launchctl bootout gui/501/<gateway-label>`; kill surviving `hermes gateway` / `hermes mcp serve` procs. Do NOT touch OrbStack (hermes-ci VM + 4 CI runners), restic, or backup LaunchAgents.
3. **Cut release manually** from main ≥ ef76057787 using the vendored cutter
   on the mini, then verify `runtime-current` repoints:

   ```bash
   ~/.hermes/runtime-current/scripts/mini-release-cut.sh \
     --ref <certified-full-sha> \
     --certified-sha <same-certified-full-sha> \
     --promotion-receipt-id <promotion-receipt-sha256>
   ```

   Do not install or bootstrap `com.colingreig.hermes.release-poll`.
4. **Fresh config** (gut, not overlay-on-old): archive live `~/.hermes/config.yaml` and state DBs into `~/hermes-archive-20260729/`; seed a NEW `config.yaml` carrying ONLY the load-bearing blocks from the snapshot copy: platforms (telegram/slack/discord/etc.), secrets, security, approvals, command_allowlist, hooks, gateway. Everything else comes from defaults + the overlay.
5. **Install bundle**: `install_fleet_config.py --dry-run` first, then real run — applies overlay (model/fallbacks/kanban), normalizes exact-pattern root legacy `config.yaml` install backups to mode `0600` with prior-mode rollback journaling, installs 5 profiles to `~/.hermes/profiles/`, replaces `cron/jobs.json` (15 jobs), and applies the manifest-pinned skill policy across the default plus five profile homes. Skill removals are archived, the two poller references are hash-verified into the kept umbrella archive, and the local vehicle-image shadow requires exact hub-lock provenance. Timestamped .baks, skill tarballs, and the receipt are automatic. Profile config backups are outside the legacy root-backup remediation.
6. **Script deps check** (review finding): every script referenced by kept jobs must exist in `~/.hermes/scripts/` — sha-compare against the release copy before starting (clickup_closeout_audit.py, stalled_task_reconciler.py, staleness_sweep.py, orphan_unpushed_cron.sh, ignite-board-sync.sh, etc.). Scripts deploy by explicit name only, never rsync.
7. **Restart gateway**: bootstrap the LaunchAgent — after bootout, poll `launchctl list` until the label clears before bootstrapping (EIO race); retry a lone EIO once.
8. **Verify** (do not skip): gateway up; `hermes profile list` shows coder/content/design/research/ops; live `~/.hermes/cron/jobs.json` contains the exact direct prompts from the bundle and neither ClickUp executor references a swarm or scratch synthesis file; then run ONE real code task and ONE real content task from ClickUp through the direct executors to In Review with handoff comments. Content task must run claude-sonnet-5 or fail — any substitution is a defect. No fresh scratch synthesis-file read may appear in the logs.
9. **Install shared Chrome CDP** in the logged-in GUI session:

   ```bash
   install -m 0755 machine-setup/mini-scripts/chrome_cdp_launch.sh \
     ~/.hermes/scripts/chrome_cdp_launch.sh
   install -m 0644 \
     machine-setup/mini-scripts/launchd/com.colingreig.chrome-cdp.plist \
     ~/Library/LaunchAgents/com.colingreig.chrome-cdp.plist
   launchctl bootstrap gui/$(id -u) \
     ~/Library/LaunchAgents/com.colingreig.chrome-cdp.plist
   ```

   Verify `lsof -nP -iTCP:9222 -sTCP:LISTEN` reports only loopback,
   `tailscale serve status` reports the TCP forward as `tailnet only`, local
   Hermes can navigate with `browser.cdp_url=http://127.0.0.1:9222`, and a
   different tailnet host can `connectOverCDP` to the browser WebSocket exposed
   by `http://100.84.74.65:9222/json/version`. Use the Tailscale IP for CDP:
   Chrome rejects the MagicDNS hostname in the DevTools `Host` header.
10. **Rollback** (if needed): installer .baks restore config/jobs; `jobs.json.pre-freeze` restores the old cron fleet; snapshot tar restores everything else. To remove shared Chrome, `launchctl bootout gui/$(id -u)/com.colingreig.chrome-cdp`; the wrapper removes its Tailscale Serve TCP forward on exit.

## Follow-ups (ClickUp rebuild-0729 tasks)
- REBUILD 5: Open Design headless (`od daemon --headless`, wrapper CLI over daemon API, Codex backend, loopback+OD_API_TOKEN). CLI-only per Colin — no MCP.
- REBUILD 9: permanent shared Chrome on mini — implemented; pending validator
  confirmation of the live launchd/CDP/VNC evidence.
- pr-staleness fold into ci-health-watch (deferred at review — script-level change).
- NOUS_PORTAL_API_KEY provisioning — done (1Password + mini op-secrets.env); tier-3 model is DeepSeek V4 Pro high (86e2jzv2j).
