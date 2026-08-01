# Hermes Mac mini — update process

This is the operator runbook for keeping the Hermes Mac mini current. Epic: ClickUp `86e2kmq8u`.

## Surfaces

| Surface | Writer | Update path |
|---------|--------|-------------|
| Hermes runtime (`runtime-current`) | `scripts/mini-release-cut.sh` | Immutable release dir + symlink swap |
| Fleet cron/config/profiles | `machine-setup/fleet-config/install_fleet_config.py` | Manifest-verified install |
| Ignite skills (external) | `ignite-skills-pull.sh` + `skills.external_dirs` | Git pull + Hermes discovery |
| Purelymail poller | `ignite-email-infra/poller/deploy-poller.sh` | Staged deploy under `~/.hermes/deploy/` |

## Standard Hermes release (code)

Run from a trusted checkout on the **mini** (or SSH and execute there):

```bash
cd ~/dev/hermes-agent   # or the checkout used for cuts
git fetch origin main
git checkout main && git pull --ff-only origin main

# Dry-run first when changing release tooling
bash scripts/mini-release-cut.sh --dry-run

# Production cut (builds ~/.hermes/releases/vX.Y.Z-<sha>/, verifies, swaps runtime-current)
bash scripts/mini-release-cut.sh
```

Verify after cut:

```bash
readlink ~/.hermes/runtime-current
~/.local/bin/hermes cron status
~/.local/bin/hermes gateway status
bash ~/.hermes/runtime-current/machine-setup/mini-scripts/verify-hermes-patches.sh
```

Rollback: `readlink ~/.hermes/releases/.previous` then repoint `runtime-current` to that release and restart gateway (see `mini-release-cut.sh` header).

## Fleet config (cron schedules, profiles, overlay)

After editing `machine-setup/fleet-config/` on main:

```bash
# If Ignite skills were ever copied into ~/.hermes/skills, remove shadows first:
python3 machine-setup/mini-scripts/cleanup_hermes_local_ignite_shadows.py --apply

# Update jobs.json sha256 in fleet_config_manifest.json, then:
python3 machine-setup/fleet-config/install_fleet_config.py --dry-run
python3 machine-setup/fleet-config/install_fleet_config.py
```

Gateway picks up `jobs.json` changes on the next ticker tick; no restart required for schedule-only changes.

## Ignite skills (external_dirs — not a local mirror)

`~/.hermes/skills` is fleet-governed (~23 manifests). **Do not rsync Ignite skills into it.**

Ignite skills live in `~/dev/ignite-skills-live` and are discovered via `skills.external_dirs`:

```bash
# Pull latest ignite-skills-live (launchd job usually handles this):
bash ~/.hermes/scripts/ignite-skills-pull.sh

# Ensure external_dirs wiring after config loss or new host:
python3 ~/.hermes/runtime-current/machine-setup/mini-scripts/reconcile_marketplace_skills.py

# Verify checkout + external_dirs + no local shadows:
bash ~/.hermes/scripts/sync_ignite_skills_to_hermes.sh
```

Receipts: `~/.hermes/state/skill-pulls/ignite-skills-live-success.json`, `~/.hermes/state/ignite-skills-external-verify.json`.

## When to update

| Trigger | Action |
|---------|--------|
| PR merged to `main` affecting runtime | `mini-release-cut.sh` |
| Cron schedule / profile / overlay change | `install_fleet_config.py` (run cleanup first if local Ignite shadows exist) |
| Ignite skills changed | `ignite-skills-pull.sh` + verify script |
| Poller code/config change | `deploy-poller.sh` from ignite-email-infra |

## Concurrency rules

- Validator + executor overlap on **different tasks** is acceptable (Hermes swarm/concurrency).
- Do not run manual `hermes cron run` on mini while scheduled LLM jobs are active unless intentional.
- One Conductor workspace owns production deploys at a time.
- Gateway restart kills in-flight cron — prefer `hermes gateway restart` over ad-hoc `--replace` from shells.
