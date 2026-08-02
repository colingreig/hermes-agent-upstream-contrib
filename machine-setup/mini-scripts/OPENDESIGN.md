# Open Design headless on the mini

Daemon: loopback Express, `127.0.0.1:7456` (`OD_PORT`/`OD_BIND_HOST`), bearer
`OD_API_TOKEN`. Client: `odx` (not `od` — `/usr/bin/od` collision); bash + curl +
`/usr/bin/python3`, no MCP, no node.

## Deploy (by explicit name, never rsync)

    /usr/bin/openssl rand -hex 32 > ~/.hermes/secrets/od_api_token   # once
    chmod 600 ~/.hermes/secrets/od_api_token
    mkdir -p ~/Library/Logs/opendesign ~/.hermes/opendesign/data
    scp odx mini:~/.hermes/bin/odx && ssh mini chmod 755 ~/.hermes/bin/odx
    scp com.hermes.opendesign.plist mini:~/Library/LaunchAgents/

The plist's daemon entry is baked in:
`/Users/colingreig/opendesign/apps/daemon/bin/od.mjs`, run with
`/Users/colingreig/opt/node24/bin/node` as `od daemon start --headless`
(imports `../dist/cli.js`). This is the standalone daemon CLI that serves a
fixed-port HTTP API (`OD_PORT` default 7456, `OD_BIND_HOST` default
127.0.0.1) matching `odx`'s expectations.

Do NOT point the entry at `apps/packaged/dist/headless.mjs` (produced by
`pnpm tools-pack mac build --to app`) — that's a different, unrelated
artifact: a full desktop-app launcher that spawns its API daemon as a
sidecar on a hardcoded ephemeral port (`DAEMON_PORT=0`) and never exposes it
on a fixed port. Confirmed by reading `apps/packaged/src/sidecars.ts` and by
a live test (it started fine but nothing ever listened on any fixed port).
No separate daemon build step is needed beyond the repo's normal build.

Then reload — never `kickstart -k`, it will not reload plist env:

    launchctl bootout gui/501/com.hermes.opendesign || true
    until ! launchctl list | grep -q com.hermes.opendesign; do sleep 1; done
    launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.hermes.opendesign.plist

A lone `Bootstrap failed: 5: Input/output error` is the launchd domain not yet
reaped — settle a few seconds, retry the bootstrap exactly once.

## Backend pinning (Codex / OpenCode)

Per run: `odx run --backend codex|opencode` sends `agentId` on `POST /api/runs`
and wins over any stored default.

Daemon default: `GET /api/app-config` returns `{config}`; `PUT /api/app-config`
with that object merged sets `agentId: "codex"` (or `"opencode"`) plus
`agentModels["codex"] = {"model": "...", "reasoning": "..."}`. Persisted to
`$OD_DATA_DIR/app-config.json`; CLI equivalent `od config set agentId codex`.

## Smoke test

    ssh mini '~/.hermes/bin/odx health'   # ok base=... version=... port=7456
    ssh mini '~/.hermes/bin/odx run-export "one-page pricing table" --backend codex --out ~/tmp/odx-smoke'
    ssh mini 'ls ~/tmp/odx-smoke/files'

Expect `run.json`, `export-manifest.json`, `<project>.zip`, `files/`. Exit: 0 ok,
1 run failed, 2 usage, 70 daemon/HTTP, 75 timeout, 78 config.
