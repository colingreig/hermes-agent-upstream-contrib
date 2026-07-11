# Per-task 1Password secret resolution — design (ClickUp 86e29q8je)

## Context & goal

Phase 2 of the secret-exfil hardening (follows 86e29q8j3, which stripped 101
non-Hermes vars and landed the current 41-var keep-list; deployed 2026-07-10,
in review).

Today the gateway resolves all 41 secrets at BOOT via
`~/.hermes/scripts/gateway_secrets_wrap.sh` → `op_sdk_resolve.py` (reads the
`~/.hermes/scripts/op-secrets.env` manifest of `KEY=op://…` refs) and bulk-exports
every resolved value into the gateway process `os.environ`
(`set -a; . resolved_env; set +a`).

Goal: eliminate long-lived business secrets from the long-running gateway
process env by resolving per-task-resolvable secrets lazily, on demand, from
an in-memory TTL cache that NEVER writes to `os.environ`, keeping only
genuinely session-scoped secrets and non-secret config boot-resident.

## Current secret flow (three layers)

1. **Boot resolution** — `gateway_secrets_wrap.sh` → `op_sdk_resolve.py`
   resolves the 41-var manifest and bulk-exports into `os.environ`. (This is
   the layer we change.)
2. **Per-call provider auth** — `hermes_cli/auth.py::_resolve_api_key_provider_secret()`
   (line 563; called from `resolve_provider()` at line 1605) already resolves
   provider keys per call, preferring `~/.hermes/.env` over `os.environ` via
   `get_env_value_prefer_dotenv()` (imported from `hermes_cli.config`, used at
   auth.py:495 and auth.py:581), with an `agent.credential_pool` fallback. So
   in-process provider keys are ALREADY looked up per-call — they do not
   fundamentally require the value to live in `os.environ`.
3. **Subprocess env build** — `tools/environments/local.py::_make_run_env()`
   (line 904) builds a spawned task's environment by copying `os.environ | env`
   wholesale, then filtering via a force-prefix → internal-secret hard-strip →
   passthrough opt-in → exact blocklist → secret-shape heuristic. External
   CLIs spawned by tasks (vercel, wrangler, git/gh) read their credentials
   from THIS child env.

## The critical distinction: in-process vs external-CLI consumers

"Per-task-resolvable" is not monolithic. It splits by CONSUMER:

- **In-process Python consumers** (Anthropic/ZAI/Gemini/MiniMax provider
  calls, ClickUp/Postmark/DataForSEO API calls made from gateway Python) can
  call a lazy resolver directly → resolve via the TTL cache, return the value
  to the caller, NEVER place it in `os.environ`.
- **External-CLI consumers** (a spawned `vercel`/`wrangler`/`git`/`gh`
  process) cannot call a Python cache — they read env vars from their own
  process environment. For these, "per-task" means: resolve at
  SUBPROCESS-SPAWN time inside `_make_run_env` and inject ONLY into that
  child process's env dict; the long-running gateway parent process still
  never holds the value.

## Classification of the 41 kept vars

### A. Session-scoped — keep boot-resident

Live long-lived connection; the tokens must be resident for the life of the
connection.

| Variable | Rationale |
|---|---|
| `SLACK_APP_TOKEN` | Slack Socket Mode holds a persistent websocket |
| `SLACK_BOT_TOKEN` | Slack Socket Mode holds a persistent websocket |

### B. Non-secret config / identifiers

Not credentials; may stay as plain env or move to a config file (no
1Password resolution needed, low exfil value).

| Variable |
|---|
| `CLICKUP_REVIEW_SLA_DRY_RUN` |
| `VALIDATE_SHADOW` |
| `HERMES_AUTONOMOUS_MERGE` |
| `HERMES_AUTONOMOUS_MERGE_HIGH` |
| `HERMES_AUTONOMOUS_MERGE_MEDIUM` |
| `HERMES_AUTONOMOUS_MERGE_LOW` |
| `HERMES_CONTENT_SONNET` |
| `HERMES_WRITER_CODEX` |
| `GLM_BASE_URL` |
| `SLACK_ALLOWED_USERS` |
| `CLOUDFLARE_ACCOUNT_ID` |
| `CLOUDFLARE_EMAIL` |
| `GH_APP_ID` |
| `GH_APP_INSTALLATION_ID` |
| `GOOGLE_APPLICATION_CREDENTIALS` (a filesystem path, not a secret) |
| `POSTMARK_HERMES_INBOUND_ADDRESS` |
| `POSTMARK_HERMES_INBOUND_SERVER_ID` |
| `POSTMARK_HERMES_INBOUND_WEBHOOK` |

Flag: confirm each of these is genuinely non-secret at implementation time;
`GOOGLE_APPLICATION_CREDENTIALS` points to a key FILE whose path can stay
resident but whose file contents are the real secret.

### C. Per-task-resolvable secrets — TTL-cache, out of `os.environ`

Sub-split by consumer.

**C1 — in-process** (TTL cache, returned to Python caller):

| Variable |
|---|
| `ANTHROPIC_API_KEY` |
| `ANTHROPIC_API_KEY_HERMES` |
| `ZAI_API_KEY` |
| `ZAI_API_KEY_HERMES` |
| `GEMINI_API_KEY` |
| `GOOGLE_API_KEY` |
| `MINIMAX_API_KEY` |
| `CLICKUP_API_TOKEN` |
| `POSTMARK_SERVER_TOKEN` |
| `POSTMARK_HERMES_INBOUND_TOKEN` |
| `DATAFORSEO_LOGIN` |
| `DATAFORSEO_PASSWORD` |
| `MCP_AGENCY_OS_API_KEY` |
| `WORKBENCH_MCP_TOKEN` |
| `CRON_SECRET` |

**C2 — external-CLI** (resolve at spawn, inject into child env only):

| Variable |
|---|
| `VERCEL_TOKEN` |
| `VERCEL_AUTOMATION_BYPASS_SECRET` |
| `CLOUDFLARE_API_TOKEN` |
| `CLOUDFLARE_API_KEY` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` |
| `GH_APP_PRIVATE_KEY` |

Ambiguous cases, called out explicitly:

- `GH_APP_PRIVATE_KEY` may be consumed both in-process (JWT mint) and by git
  — treat as C2 (spawn-inject) to be safe.
- `CLOUDFLARE_API_KEY` vs `CLOUDFLARE_API_TOKEN` — confirm which
  wrangler/CI path actually uses which at implementation time.

## Proposed architecture

A new lazy resolver module (e.g. `hermes_secrets/lazy_resolver.py`) wrapping
`op_sdk_resolve.resolve_refs()`: `get(name) -> value|None`, keyed by the
var's `op://` ref (read once from the `op-secrets.env` manifest at boot —
refs are not secret), backed by an in-memory dict cache with a per-entry TTL
(suggest 5–15 min; align to `op_sdk_resolve`'s own behavior) and a
single-flight lock so concurrent tasks don't stampede 1Password. NEVER calls
`os.environ.__setitem__`.

### Integration points

1. `hermes_cli/auth.py::_resolve_api_key_provider_secret` (line 563) — add
   the lazy resolver as a resolution source (after the `.env`-preferred
   lookup via `get_env_value_prefer_dotenv`, before/as a replacement for the
   raw `os.environ` read) for C1 vars.
2. `tools/environments/local.py::_make_run_env` (line 904) — for C2 vars,
   resolve at spawn via the lazy resolver and inject into the returned child
   env dict. These are intentionally scoped to the child process, not the
   gateway parent.
3. Boot (`gateway_secrets_wrap.sh` / gateway startup) — stop bulk-exporting
   C1+C2 secrets into `os.environ`; export ONLY bucket A (session-scoped) and
   bucket B (non-secret config). The manifest of `op://` refs is still read
   at boot (refs, not values) to seed the resolver's ref map.

### TTL / refresh

A rotated secret in 1Password becomes live within one TTL window without a
gateway restart — a side benefit over today's boot-only resolution (cf. the
recurring stale-`.env`-beats-rotated-1P-secret class of incidents).

## Acceptance criteria (from the task) & verification

1. **No long-lived business secret in the gateway process env post-boot** —
   verify with a script that dumps `os.environ` shortly after boot and diffs
   the key set against the expected minimal set (bucket A + bucket B only:
   `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, plus the 18 bucket-B vars listed
   above). Any C1/C2 key present in that dump is a fail.
2. **This design doc enumerates the post-86e29q8j3 keep-list and classifies
   each var boot/session-scoped vs per-task-resolvable** — done above.
3. **Implementation bypasses `os.environ` entirely for C1** (in-memory TTL
   cache).

## Risks & rollout

- **Blast radius: fleet-wide.** A bug in the resolver breaks provider auth
  for EVERY cron/task. Deploy via a supervised caretaker session with the
  gateway watched, NOT an unattended cadence run. Keep a one-flag fallback to
  the old boot-export path for instant rollback.
- **Dependency:** 86e29q8j3 is only *in review* (deployed but not
  validator-confirmed). Do not begin the fleet-wide implementation until j3
  clears validation, to avoid building on an unsettled keep-list.
- **`op_sdk_resolve` constraints:** SDK-only (never shell to `op` — caused a
  2026-07-05 boot-crash-loop); values may be quote-wrapped (`KEY="val"`) —
  the resolver must strip wrapping consistently so non-bash consumers don't
  get poisoned values.
- **Staged rollout:**
  1. Land this doc.
  2. Build + unit-test the resolver in isolation.
  3. Migrate C1 provider keys behind a flag, caretaker-verify.
  4. Migrate C2 spawn-injection.
  5. Flip boot export off for C1/C2.
  6. Run the `os.environ`-dump verification.
