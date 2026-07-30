# Hermes Rebuild Cutover — paused 2026-07-29 (mini power outage)

Resume this runbook when the Mac mini has power. Session context: Colin ordered a full gut-and-rebuild of the Hermes fleet on 2026-07-29. Code side is DONE (PR #210, merge sha ef76057787). Only the mini-side cutover remains.

## State when paused
- **PR #210 merged to main**: this `machine-setup/fleet-config/` bundle — 5 profiles, 16-job jobs.json, config-overlay.yaml, sha256-pinned `install_fleet_config.py` (+ tests), README. Survived opus adversarial review; polling flow / content synthesizer / rollback scope all fixed and test-proven.
- **Mini**: all 36 old crons frozen (`enabled=false`); full snapshot at `~/hermes-archive-20260729/` (hermes-state.tgz = config.yaml, cron, scripts, LaunchAgents, opencode.jsonc; plus jobs.json.pre-freeze). The OLD gateway was still running when power died — after power restore, auto-login will relaunch it via LaunchAgent. Shut it down before anything else.
- **ClickUp** (list 901714465284): board cleared (108 old tasks closed); 10 REBUILD tasks tagged `rebuild-0729` (86e2jbbey…86e2jbbtv); 3 preserved non-hermes tasks.
- **Fallback chain**: openai-codex/gpt-5.5 → zai/glm-4.7 → nous/moonshotai/kimi-k2.6 (`inference-api.nousresearch.com/v1`). NOUS_PORTAL_API_KEY not yet provisioned — tier 3 inert until Colin adds it to 1Password + gateway secret env (guard with `${NOUS_PORTAL_API_KEY:-}` — wrapper runs `set -u`).
- **Hard rules**: codex-oauth is the only OpenAI surface (billed openai-api banned everywhere); content = anthropic/claude-sonnet-5 fail-closed, NO fallback (verified: profile sessions do not inherit root fallback_providers); ClickUp is the sole board — kanban swarm is internal execution only; tasks go to In Review, never Complete (ignite-validate owns Complete).
- **Production release path**: the rebuilt fleet uses manual governed release
  cuts only. `com.colingreig.hermes.release-poll` is retired for this fleet and
  is not installed, loaded, or enabled. Every production cut must name the
  exact certified full SHA as both `--ref` and `--certified-sha`, plus the
  immutable promotion receipt ID via `--promotion-receipt-id`. Generic poller
  tooling remains in the repo only as an explicitly adopted contingency for
  other deployments.

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
5. **Install bundle**: `install_fleet_config.py --dry-run` first, then real run — applies overlay (model/fallbacks/kanban), installs 5 profiles to `~/.hermes/profiles/`, replaces `cron/jobs.json` (16 jobs). Timestamped .baks + receipt are automatic.
6. **Script deps check** (review finding): every script referenced by kept jobs must exist in `~/.hermes/scripts/` — sha-compare against the release copy before starting (clickup_closeout_audit.py, stalled_task_reconciler.py, staleness_sweep.py, orphan_unpushed_cron.sh, ignite-board-sync.sh, etc.). Scripts deploy by explicit name only, never rsync.
7. **Restart gateway**: bootstrap the LaunchAgent — after bootout, poll `launchctl list` until the label clears before bootstrapping (EIO race); retry a lone EIO once.
8. **Verify** (do not skip): gateway up; kanban dispatcher ticking (60s, `kanban.dispatch_in_gateway`); `hermes profile list` shows coder/content/design/research/ops; one throwaway swarm end-to-end (`hermes kanban swarm "smoke test" --worker coder:"say hi" --verifier ops --synthesizer coder --json`, poll `hermes kanban show <synthesizer_id> --json` until done); then ONE real code task and ONE real content task from ClickUp through swarm to In Review. Content task must run claude-sonnet-5 or fail — any substitution is a defect.
9. **Rollback** (if needed): installer .baks restore config/jobs; `jobs.json.pre-freeze` restores the old cron fleet; snapshot tar restores everything else.

## Follow-ups (ClickUp rebuild-0729 tasks)
- REBUILD 5: Open Design headless (`od daemon --headless`, wrapper CLI over daemon API, Codex backend, loopback+OD_API_TOKEN). CLI-only per Colin — no MCP.
- REBUILD 9: permanent shared Chrome on mini, CDP bound tailnet-only, other PCs connect via connectOverCDP.
- pr-staleness fold into ci-health-watch (deferred at review — script-level change).
- NOUS_PORTAL_API_KEY provisioning (Colin).
