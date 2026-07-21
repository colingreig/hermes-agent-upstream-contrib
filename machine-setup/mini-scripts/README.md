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

**Convention going forward:** any `~/.hermes/scripts/*.py` file that fixes a
production incident gets a canonical copy committed here, in git, so it
survives even a full home-directory loss — not just a `~/.hermes/local-patches`
copy (that directory itself was lost in the same incident).

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
