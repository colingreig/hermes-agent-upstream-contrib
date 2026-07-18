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
