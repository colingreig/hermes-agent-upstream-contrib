# Audit: headless 1Password CLI hang and daemon backlog

## TL;DR

- On the Mac mini, a headless `open(2)` of the 1Password app-group containers blocks indefinitely; `op` 2.34.1 probes `~/Library/Group Containers/2BUA8C4S2C.com.1password` and inherits that block.
- Four abandoned Hermes terminal commands were stuck inside the ClickUp CLI's bare `op read` fallback; repeated invocations had accumulated 484 detached `op daemon` processes.
- A disposable HOME avoids the app-group probe. A 15-second process-group timeout plus an owned scratch daemon prevents both hangs and detached-daemon accumulation.
- The 1Password SDK path is unaffected and remains the preferred integration for long-lived Hermes services.
- Canonical remediation is in [ignite-skills PR #41](https://github.com/colingreig/ignite-skills/pull/41); zero additional ClickUp tasks were created because task `86e29zy5n` already owns the work.

## Findings

### High — headless app-group traversal blocks in `open(2)` (CONFIRMED)

The fault follows the mini's real HOME, not the saved `op` configuration or its daemon socket:

- A controlled `op read` with the real HOME exceeded a four-second deadline; the same command with an empty temporary HOME returned the secret in about 1.65 seconds.
- An empty HOME with `OP_CONFIG_DIR` pointed at the real `~/.config/op` also completed, ruling out `~/.config/op/config` and `op-daemon.sock` as the blocking object.
- Symlinking only `Library` into an empty HOME reproduced the hang; `Desktop`, `Documents`, `Downloads`, `.config`, `.ssh`, `.local`, `.cache`, and `.hermes` did not.
- Within `Library`, only `Group Containers` reproduced it. Within that directory, the current `2BUA8C4S2C.com.1password` container reproduced the `op` hang; the legacy AgileBits container was not probed by `op`.
- A sampled hung `op` thread remained in `open` / `__open`. A direct signal-bounded `os.open(path, O_RDONLY|O_DIRECTORY)` on both 1Password app-group directories timed out after 3.00 seconds on the mini even though `lstat` succeeded. The same direct opens completed immediately on the MacBook.

This is a macOS headless privacy/app-container access failure: metadata lookup succeeds, but opening the protected app-group directory from the mini's SSH/Hermes context never returns. Scratch HOME is not merely a workaround for stale CLI config; it prevents the desktop-integration discovery path from existing.

**Action:** Every unattended CLI call now uses a disposable HOME. The runbook records the exact blocked path and forbids new bare `op read` fallbacks.

### High — ClickUp fallback plus abandoned Hermes terminals spawned the backlog (CONFIRMED)

Before cleanup, the mini had 484 `op daemon --background` processes and four live `op read op://Dev Toolbox/dev/CLICKUP_API_TOKEN` clients. The four clients all began at `2026-07-12 02:56:04` and had these direct parents:

- `node ~/.claude/skills/clickup/clickup.mjs comments 86e1z37j4`
- `node ~/.claude/skills/clickup/clickup.mjs comments 86e29qatv`
- `node ~/.claude/skills/clickup/clickup.mjs task 86e29q8kd --json`
- another ClickUp `comments` call for `86e29qatv`

Their process-group roots were abandoned Hermes terminal commands 16–29 hours old. The ClickUp fallback used synchronous `execFileSync('op', ...)` with no timeout, so each parent waited forever. Each `op` client also forked a detached daemon; the CLI help confirms that a daemon remains alive for 24 hours of inactivity by default. Killing or timing out only the client therefore cannot prevent the backlog.

**Action:** The four proven process groups, all CLI clients, and all 484 daemons were killed; the stale real-HOME socket was removed. The new guard pre-starts one daemon on a unique scratch `OP_SOCK`, records its PID, and kills it in cleanup. A post-cleanup `op-safe read` and ClickUp fallback both completed in about two seconds with zero daemons immediately afterward and after an additional two-second check.

### Medium — direct CLI use existed beyond ClickUp (CONFIRMED)

The canonical sweep found direct `op` subprocesses in:

- ClickUp CLI, closeout audit, and cross-board review sweep;
- `op-run`, `op-write`, and the shell secret-cache refresh;
- site-downloader and site-replicator secret helpers; and
- LinkedIn ad-inventory secret resolution.

All are covered by [ignite-skills commit `d659e00`](https://github.com/colingreig/ignite-skills/commit/d659e00): shell callers route through `op-safe`; Node and Python callers use equivalent bounded scratch-HOME helpers. The ClickUp helper has tests for disposable-HOME cleanup and deadline enforcement.

The task's `sync.sh` lead was checked but did not hold: the canonical macOS sync script does not invoke `op`. SDK-migrated Hermes wrappers were also intentionally left unchanged.

### Info — SDK secret resolution is outside the failure boundary (CONFIRMED)

`~/.hermes/scripts/op_sdk_resolve.py` resolved the same ClickUp secret under the mini's real HOME in 1.65 seconds. It did not create an `op` CLI process or daemon. Gateway secret resolution therefore remains on the safe side of the boundary.

## Dead ends

- `~/.config/op/config` content and the real `op-daemon.sock` were not causal; using that configuration under an empty HOME completed.
- `OP_BIOMETRIC_UNLOCK_ENABLED=false` alone was insufficient; the blocked app-group traversal still occurred.
- The stale socket explained coordination noise but not the `open(2)` block.
- The canonical macOS `sync.sh` contains no `op` call and was not a spawner.
- Long-lived `ssh mini … hermes mcp serve` processes were a separate transport population and were not killed.
- Unified TCC logs recorded `op` user lookups but did not expose the blocked pathname; direct `os.open` isolation supplied the path-level proof instead.

## Remediation and validation

- Live mini guard installed at `~/.local/bin/op-safe`.
- Live ClickUp and closeout paths patched in `~/.claude/skills/clickup` and the active `2.36.2` plugin cache.
- Live `verify-writer-chain.py` switched from bare `op read` to `op-safe`.
- Canonical changes pushed in [ignite-skills PR #41](https://github.com/colingreig/ignite-skills/pull/41).
- Validation: 22 Node tests passed; all modified Node files passed syntax checks; both Python callers compiled; all shell callers passed `bash -n`; live guarded reads completed with no residual daemon or CLI process.

## Method

| Dimension | Method | Result |
|---|---|---|
| Filesystem isolation | HOME/config/top-level/Library/app-group differential probes | Exact failing discovery path isolated |
| Syscall confirmation | `sample`, signal-bounded direct `os.open` | Blocking syscall and target confirmed |
| Process provenance | PID/PPID/PGID chains and command lines | ClickUp fallback and abandoned Hermes groups identified |
| Caller inventory | Canonical and live `rg` sweeps, excluding backups/docs | Every executable direct caller covered |
| Boundary check | Real SDK resolution against the same secret | SDK unaffected |
| Adversarial checks | Config/socket/sync-script counter-hypotheses | Refuted and recorded above |

This was a single-agent investigation because the Codex runtime permits audit fan-out only when the user explicitly requests subagents; all investigation lenses were run sequentially.
