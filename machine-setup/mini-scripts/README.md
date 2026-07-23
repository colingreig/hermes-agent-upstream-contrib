# Mini-local scripts (canonical copies)

These scripts run on the Mac mini at `~/.hermes/scripts/` but live **outside**
the mini's git-tracked release/runtime-current deploy path (see
`hermes_cli/gateway.py` / `hermes update`) — nothing in this repo's deploy
pipeline provisions, copies, or regenerates `~/.hermes/scripts/*`. That
independence is normally fine, but it also means these files have no backup
story beyond the mini's own `.bak-*` files and whatever restic snapshot
happens to be current.

The 2026-07-19 mini home-directory data-loss incident (see ClickUp 86e2ddcpb)
proved that gap real: the `op_sdk_resolve.py` resilience patch (300s cache +
retry/backoff + serve-stale, added 2026-07-13 after a ~13h 1Password
daily-quota lockout) was silently lost in the wipe/recovery and nobody
noticed until this task re-verified it (86e2a99q9, 2026-07-21).

**Convention going forward:** any `~/.hermes/scripts/*` file (`.py` or `.sh`)
that fixes a production incident gets a canonical copy committed here, in git,
so it survives even a full home-directory loss — not just a
`~/.hermes/local-patches` copy (that directory itself was lost in the same
incident).

To restore a script after any kind of mini data loss:

```bash
scp machine-setup/mini-scripts/<file> mac-mini-h.tail51ec1b.ts.net:~/.hermes/scripts/<file>
mini-run -- 'python3 -m py_compile ~/.hermes/scripts/<file>'  # sanity check
```

Diff against the live file periodically (`ssh mini cat ~/.hermes/scripts/<file>`
vs this copy) to catch drift — nothing currently automates that check.

## Files

- `op_sdk_resolve.py` — resolves `op://` secret references via the 1Password
  service-account SDK for `gateway_secrets_wrap.sh` and cron/sentinel scripts.
  Restored 2026-07-21 with the HERMES-PATCH 31 resilience layer (cache,
  retry/backoff, serve-stale, id-fast-path) re-added from the original spec
  in ClickUp 86e2a99q9 after the 2026-07-19 loss; live-verified (142/142
  secrets resolved, cache hit confirmed on a second run, 0700/0600 perms).
  Hardened for ClickUp 86e2a2paz on 2026-07-23: auth/unauthorized/invalid/
  forbidden/expired/token markers take precedence over transient-looking text;
  transient failures use three bounded jittered retries around 5/15/45 seconds;
  exhausted transient failures serve stale only when every requested value has
  complete usable cache data, otherwise they retain the CLI's exact FATAL/exit-1
  contract. Importable `resolve_refs()` consumers now use the same retry/cache
  path, while stdout remains byte-compatible `KEY="value"` shell input.
- `sentinel_run.sh` — launchd runner for Ignite Sentinel. De-clusters its
  1Password resolve with a random delay in the inclusive 0–120 second window.
  `SENTINEL_START_DELAY_MAX_SECONDS=0 SENTINEL_SMOKE_ONLY=1` performs the
  secrets-resolution smoke without running the monitor or emitting Slack.
- `degraded_secrets_monitor.py` — detects repeated secret-resolution failures
  and unresolved placeholders. Its SDK subprocess uses the immutable
  `~/.hermes/runtime-current/venv/bin/python` path, not the removed mutable
  `~/.hermes/hermes-agent/venv/bin/python` checkout.
- `tests/test_op_sdk_resolve.py` — fully mocked resolver contract harness:
  transient-then-success, exhausted transient without stale, mixed auth +
  timeout precedence, complete stale fallback, and stdout quoting bytes.
- `tests/test_sentinel_run.sh` — validates the 0–120 second delay contract and
  executes a fake-HOME, Slack-silent sentinel smoke.
- `tests/test_op_sdk_consumers.sh` — verifies canonical/live resolver byte
  identity plus the sentinel, degraded-monitor, and marketplace consumer paths.
- `verify-hermes-patches.sh` — idempotent guard/health-check for the 12 legacy
  hand-patches (now all formally merged to main) plus ~30 other live-deploy
  sentinels (GH App token, marketplace sync cron, validator model chain,
  skills freshness, DB-publish lane, etc). Fixed 2026-07-22 (ClickUp 86e2e7z2h)
  to stop hardcoding the pre-2026-07-19 mutable `$HOME/.hermes/hermes-agent`
  checkout — `REPO` now resolves `$HOME/.hermes/runtime-current` (the current
  immutable release). Since the original `.patch` diff files were lost in the
  same 2026-07-19 wipe and are unrecoverable, patch verification is now
  sentinel-first (grep a load-bearing string in the live release) rather than
  `git apply --reverse --check` against a file that no longer exists; a `.patch`
  file, if one is ever added back to `~/.hermes/local-patches`, still gets the
  git-apply re-application path. Before this fix the script exited at `cd
  "$REPO"` before reaching ANY of its ~30 other checks — those were silently
  unverifiable since 2026-07-19, not merely "assumed green".
- `offbox_restic_backup.py` — nightly restic backup of `~/.hermes` to
  Cloudflare R2. `BACKUP_TARGETS` added `~/.hermes/memories` 2026-07-22
  (ClickUp 86e2e870p) after discovering it had never been in scope — the
  2026-07-19 wipe permanently lost Hermes's entire MEMORY.md/USER.md
  personalization with zero restic snapshot history to restore from, at any
  point. This closes the gap for future incidents; it does not recover what
  was already lost (see 86e2e870p for the reseed decision, separately pending
  Colin's input).
