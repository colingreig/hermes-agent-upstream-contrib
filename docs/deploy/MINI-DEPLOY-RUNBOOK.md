# Hermes Mini deploy runbook — the one deployment path

Task 86e2m2c1g. This is the single runbook for getting any change onto the
production Mac mini. Every file the runtime executes from `~/.hermes/scripts/`
or `~/Library/LaunchAgents/` is in exactly one of four classes, declared
machine-readably in `machine-setup/mini-scripts/mini_local_registry.json`. A
live file in none of them trips the `deployment-coverage` operational check in
`fleet_outcome_probe.py` (Slack alarm, code `undeclared_file`).

Companion docs this runbook routes to (it does not replace them):
`scripts/MINI-RELEASE.md` (the release-cut mechanics) and
`machine-setup/mini-scripts/README.md` (per-bundle installer details).

## The four classes

| Class | Declared where | Deployed by | Drift enforcement |
| --- | --- | --- | --- |
| 1. Bundle-governed | A bundle manifest (`spend_manifest.json`, `self_report_manifest.json`, `disk_lifecycle_manifest.json`, `github_app_manifest.json`, `fleet_outcome_manifest.json`, `pr_pipeline/manifest.json`, `fleet-config/fleet_config_manifest.json`) | Its installer/reconciler, most of them run by `scripts/mini-release-cut.sh` | Manifest sha256 at install; `governed-path-integrity` + `deployment-coverage` checks (`bundle_sha_drift`) |
| 2. Direct-deploy | `direct_deploy` in `mini_local_registry.json` | `scp` **by explicit name** from the repo to the live path (never rsync) | Live bytes must equal the `runtime-current` release mirror (`direct_deploy_drift` / `direct_deploy_missing`) |
| 3. Mini-local | `mini_local` in `mini_local_registry.json`, with category + reason | Never deployed from the repo (secrets, mini-only ops scripts, third-party agents) | Coverage only; deleting one without updating the registry alarms `registry_stale_entry` |
| 4. Runtime state / debris | `state` patterns in the registry (`.…json` state, `__pycache__`, `*.bak*`, …) | n/a | Ignored, never alarmed |

## Shipping a change

The release cut runs a fail-closed admission check immediately after resolving
the active and target commits, before build or `runtime-current` mutation. Any
changed file under `machine-setup/mini-scripts/` that is outside all deploy
manifests/cutter-owned paths rejects the cut; there is no warning-only release.
Classify and govern the file first, then rerun the cut.

**A. File already governed by a bundle manifest.** Edit the source under
`machine-setup/mini-scripts/` (or `machine-setup/fleet-config/`), update the
bundle manifest's `sha256` for that file, merge, cut a release
(`scripts/mini-release-cut.sh` — see MINI-RELEASE.md). The cut runs the
reconcilers (`reconcile_fleet_outcomes.py`, `reconcile_pr_pipeline.py`,
`reconcile_launchd_environment.py`, `reconcile_marketplace_skills.py`,
`install_fleet_config.py`). The `install_*.py` bundle installers (spend,
self-report, disk-lifecycle, github-app) are run explicitly after the cut when
their bundle changed. **Checksum trap:** editing a governed file without
regenerating its manifest sha256 silently aborts/rolls back the cutover
(hermes-fleet-outcome-manifest-hash-drift incident) — CI test
`tests/machine_setup/test_mini_local_registry.py` pins the fleet-outcome ones.

**B. File in `direct_deploy`.** Edit the repo source, merge, cut (so the
release mirror advances), then copy the file to its live path by explicit
name: `scp machine-setup/mini-scripts/<src_rel> mini:<dest>`. Until you do,
`deployment-coverage` alarms `direct_deploy_drift`/`direct_deploy_missing` —
that alarm staying red is the fix for the 86e2hap1g class where merged fixes
never shipped. Vendor-verbatim-then-change applies: if the live copy is ahead
of the repo, vendor the live bytes first.

**C. New file for the mini.** Choose its class *before* it lands:
- Belongs to an existing bundle → add it to that bundle's manifest `files`
  list (with sha256) and ship via path A.
- Standalone repo-owned script/plist → add the file under
  `machine-setup/mini-scripts/` (plists under `launchd/`) **and** add a
  `direct_deploy` entry (`src_rel` + `dest`) to `mini_local_registry.json`.
- Deliberately mini-only (secret, env-specific service, mini-only ops
  script) → add a `mini_local` entry with `category` and `reason`.
Then regenerate the registry's sha256 in `fleet_outcome_manifest.json`
(the registry itself deploys as a fleet-outcome bundle file).
Skipping this step is what the `undeclared_file` alarm exists to catch.

**D. Config / profiles / cron jobs.** Not file-copy at all: the fleet-config
bundle (`machine-setup/fleet-config/`) owns `config.yaml` (overlay), the
profiles, and `cron/jobs.json`. Never hand-edit those on the mini; hand edits
get clobbered by the next cut and alarm via `verify_governed_paths.py`.

## Responding to deployment-coverage alarms

| Code | Meaning | Action |
| --- | --- | --- |
| `undeclared_file` | Live file in no class | Classify it (path C) or delete it on the mini |
| `bundle_sha_drift` | Bundle file's live bytes ≠ manifest sha | Rerun the bundle installer, or vendor the live bytes if live is canon |
| `direct_deploy_drift` | Direct file ≠ release mirror | `scp` the repo copy by name (or vendor live bytes first if live is ahead) |
| `direct_deploy_missing` | Declared direct file absent live though released | `scp` it by name |
| `mirror_source_missing` | Live direct file whose repo source vanished | Restore the source or reclassify the file |
| `registry_stale_entry` | Declared mini-local file no longer exists | Remove the entry, regen the registry sha in `fleet_outcome_manifest.json` |
| `coverage_registry_missing` / `coverage_registry_invalid` / `coverage_manifest_unreadable` | The coverage machinery itself is broken | Fix the registry/manifest deployment before trusting anything else |

`pinned_exceptions` accept one known-drifted sha per file so the alarm stays
satisfiable while reconciliation is tracked (currently task 86e2m63ua); a pin
never suppresses *further* drift.

## Durability notes

Restic/R2 backs up `~/.hermes/{config.yaml,cron/jobs.json,scripts,hermes-agent,state.db,db-backups}`
but **not** `~/Library/LaunchAgents` or `~/.config/opencode/opencode.jsonc`.
Every LaunchAgent the fleet depends on must therefore be class 1 or 2 (repo
plists under `machine-setup/mini-scripts/launchd/`) — class-3 plists
(gateway, dashboard, sentinel, …) survive only in the registry declaration and
backups of convenience, which is a known accepted risk recorded per entry.
