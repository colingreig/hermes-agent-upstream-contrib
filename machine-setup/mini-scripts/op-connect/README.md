# 1Password Connect — local secrets server (Mac mini)

Set up 2026-07-24 to move Hermes/mini secret resolution off the **cloud**
1Password service account (which is rate-limited — the ~13h daily-quota lockout
that `op_sdk_resolve.py`'s HERMES-PATCH 31 cache was built to survive) and onto a
**local** Connect server that serves from a locally-synced copy of the granted
vaults: no cloud rate limit, no `op` CLI, no desktop-app approval prompt.

## What runs where

- **Connect server**: name `hermes-mini` (UUID `4D3GRJEY2JAY7EO6NMENJVLOF4`),
  granted the `Dev Toolbox` (`2e27wokiplopef562fzohfcxwy`) and `hermes-agent`
  (`hi4tyqzqdpxkjgvaymsugm56ya`) vaults — the only two the 144 `op://` refs in
  `~/.hermes/scripts/op-secrets.env` span.
- **Containers** (OrbStack, `restart: always`, bound to `127.0.0.1:8080` only):
  `op-connect-api` + `op-connect-sync`, from `docker-compose.yml` in this dir.
  Live copy: `~/.config/op-connect/docker-compose.yml`.
- **Credentials / token** (0600, NOT in git): `~/.config/op-connect/1password-credentials.json`
  and `~/.config/op-connect/connect-token` (JWT, scoped to both vaults).
- **OrbStack** `app.start_at_login` is enabled so Connect resumes after a reboot.

## How Hermes + the CLI use it

- `op_sdk_resolve.py` (canonical copy one dir up) is **Connect-first**: it resolves
  via the local Connect server (auto-detected from `OP_CONNECT_HOST` or the token
  file, defaulting host to `http://localhost:8080`) and falls back to the cloud
  service-account SDK only when Connect is down or a ref is outside the token's
  vault scope. Verified: 142/142 refs resolve via Connect with the cloud token
  removed.
- `gateway_secrets_wrap.sh` and `~/.zshenv` export `OP_CONNECT_HOST`/`OP_CONNECT_TOKEN`
  so raw `op read`/`op run` calls (gateway agent tasks; interactive shells) also
  use Connect — no approval prompt. Note: `op` management commands like
  `op vault list` are not Connect-compatible and need those vars unset.

## Durability (critical)

Future release venvs get the Connect SDK ONLY because `onepasswordconnectsdk` is
declared in the repo's `pyproject.toml` + `uv.lock`. If it were installed only by
hand into a release venv, the next `uv sync --locked` rebuild would silently drop
it and every boot would revert to the rate-limited cloud path (exactly what the
`onepassword-sdk` comment in `pyproject.toml` records happening on 2026-07-08).

## Restore after a mini wipe

```bash
# 1) bring back credentials + token (from a secure backup — they are NOT in git)
#    into ~/.config/op-connect/ at 0600
# 2) restore the compose file and start the containers
cp machine-setup/mini-scripts/op-connect/docker-compose.yml ~/.config/op-connect/
(cd ~/.config/op-connect && docker-compose up -d)
# 3) confirm health + both vaults reachable
curl -s http://localhost:8080/heartbeat
# If the Connect server/token itself was lost, recreate via the op CLI:
#   op connect server create hermes-mini --vault <DevToolboxID> --vault <hermes-agentID>
#   op connect token create hermes-mini-both --server hermes-mini \
#       --vault <DevToolboxID> --vault <hermes-agentID>   # repeated --vault, NOT comma
```
