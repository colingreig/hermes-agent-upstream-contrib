# RCA: Mini Outage — 2026-07-14/15

## Summary
ClickUp task 86e2bjabd reported the mini unreachable with a claimed onset ~22:00Z on 2026-07-14. Direct log inspection does not support that timestamp; the corrected timeline places the outage ~2h earlier, ending in a self-recovered reboot.

## Corrected timeline (evidence-cited)
- Normal log activity continued through 20:10 PDT 2026-07-14 (03:10Z Jul 15) — last ordinary activity before the gap.
- The mini rebooted at 20:19 PDT 2026-07-14 (03:19:28Z Jul 15), 9 min after last activity. Confirmed via `last reboot`: `reboot time  Tue Jul 14 20:19`.
- Unlike the two prior reboots (Jul 5 13:17, Jul 3 10:11), each with a matching `shutdown time` entry immediately before, the Jul 14 20:19 reboot has NO preceding shutdown-time entry — the signature of an ungraceful stop (power loss or full hang), not a clean restart.
- `pmset -g` shows `autorestart 1`, so macOS powers back on automatically after unexpected power loss — consistent with self-recovery shortly after 20:19 PDT.

## Discrepancy with the claimed onset
The ~22:00Z claim is not supported. The activity gap starts ~03:10Z (20:10 PDT) and the box was back by ~03:19Z (20:19 PDT) — a ~9-minute window, ~2h earlier than claimed. The original estimate was likely based on when the absence was first noticed/escalated, not when the mini actually went down.

## Conclusion
Root cause: an ungraceful power interruption (no clean shutdown record) at ~20:10-20:19 PDT on 2026-07-14, self-recovered via `pmset autorestart`. No manual intervention required. This RCA motivates the external heartbeat dead-man's switch (scripts/hermes_heartbeat.sh + scripts/launchd/com.colingreig.hermes.heartbeat.plist) so future outages are detected independent of the mini's own ability to self-report.

## The larger impact: the ~53h scheduled-monitor blackout (reboot-without-login)
The 9-minute power gap is not the significant part of this incident. The box came back at 20:19 PDT, but macOS booted straight to the login window and **no console/Aqua login happened until 2026-07-17 01:47 PDT — a ~53-hour (~2.5-day) window** during which the mini looked "up" (SSH, gateway, dashboard all reachable) while an entire class of scheduled work silently did not run.

**Mechanism.** The Hermes scheduled monitors run as per-user LaunchAgents in the `gui/501` domain, which macOS only bootstraps while a console session is active. Auto-restart brings the *machine* back but not a *login session*, so any GUI-domain job without `RunAtLoad` was never scheduled:

- **Silently dead the whole window** (StartInterval / StartCalendarInterval, no `RunAtLoad`, `runs = 0`): `com.colingreig.hermes.ignite-sentinel`, `…ignite-sentinel-digest`, `…daily-spend-alert`, `…worktree-backstop-sweep`, `com.hermes.offbox-restic-backup`.
- **Recovered fine** (`RunAtLoad` in an already-loaded domain): gateway, dashboard, degraded-secrets-monitor.

This is why the outage's real cost was hidden: the always-on services self-recovered and reported healthy, so nothing flagged that the monitoring/backup fleet — including the very sentinel meant to catch outages — was itself offline for 53h. A reboot-without-login is indistinguishable from "the monitors are broken" without checking `launchctl print gui/501/<label>` (`runs = 0 / last exit code = (never exited)`) against `last reboot` + `who`.

**Diagnostic signature.** GUI-domain scheduled jobs at `runs = 0` while `RunAtLoad` jobs show `runs = 1` after a fresh login; `sysctl kern.boottime` / `last reboot` shows a reboot with no subsequent `who` login until much later.

**Fix applied 2026-07-17.** FileVault is OFF (`fdesetup status`), so the disk unlocks at cold boot and macOS can auto-login without interaction. Enabled auto-login for `colingreig` so an Aqua session (and `gui/501`) always comes up after any reboot — **zero job-plist changes, fixes every GUI-domain agent at once**. From a non-console SSH session, plain `sudo sysadminctl` fails with `error:22`; it must be wrapped in `launchctl asuser 501`:

```
sudo launchctl asuser 501 sysadminctl -autologin set -userName colingreig -password <secret>
```

This wrote `autoLoginUser=colingreig` to `/Library/Preferences/com.apple.loginwindow` + `/etc/kcpassword` (0600); backup at `…loginwindow.plist.bak-autologin-20260717`. **Rollback:** `sudo sysadminctl -autologin off`. Security delta is contained (FileVault was already OFF; no new network surface). A LaunchDaemon migration was viable but rejected as more invasive for no benefit. The kcpassword was XOR-decoded and SHA-256-matched to the real password, so boot will not stall at a login prompt — though it has not been exercised through a real reboot yet.

**Residual gap.** The two controls added from this incident are complementary: the external heartbeat dead-man's switch catches the *machine* going away; auto-login catches the *session* not coming back. Neither yet actively asserts "the GUI-domain scheduled fleet ran on schedule" — a reboot-without-login before 2026-07-17, or any future regression of the auto-login setting, would still produce a silent scheduled-monitor blackout until the next heartbeat gap or manual `launchctl print` check surfaces it.

## Verification: deliberate simulated-miss (2026-07-18)

To confirm the dead-man's switch actually alerts (not just that it pings), a deliberate miss was triggered against the live healthchecks.io check backing `scripts/hermes_heartbeat.sh` on the mini.

- **Deliberate failure ping** — sent from the mini via `mini-run`:
  ```
  curl -sS -w "\nHTTP %{http_code}\n" https://hc-ping.com/cf7a558e-ef46-4222-88b8-d92029dfe970/fail
  ```
  Response: `OK` / `HTTP 200`, at **2026-07-18T01:50:50Z** (UTC, from the mini's own clock).

  Hitting the `/fail` endpoint tells healthchecks.io to immediately flip the check to the DOWN state and fire its configured alert — an email to **colin@ignitemarketing.com** — regardless of the normal 10-min period / 30-min grace window. There is no read API key configured for this check, so the alert email landing in Colin's inbox is the actual proof of end-to-end delivery; it is not independently scriptable from the mini and is not captured in this repo.

- **Recovery ping** — sent immediately after, to restore healthy state and avoid leaving the check (and the on-call alert) in a failed condition:
  ```
  curl -sS -w "\nHTTP %{http_code}\n" https://hc-ping.com/cf7a558e-ef46-4222-88b8-d92029dfe970
  ```
  Response: `OK` / `HTTP 200`, at **2026-07-18T01:50:57Z** (UTC). This is the same plain ping the launchd job (`StartInterval=600`) sends every 10 minutes in normal operation; healthchecks.io treats it as an "up" signal and clears the DOWN state.

- **Result**: the check was DOWN for ~7 seconds (01:50:50Z → 01:50:57Z) before recovery. The one artifact this verification does *not* produce locally is the alert email itself — that is healthchecks.io's side-effect, delivered directly to Colin's inbox, and is the real evidence that the dead-man's switch is wired end-to-end.
