# Hermes Mini skill-policy recovery

`skills-policy.json` is intentionally narrower than Hermes's global
`.no-bundled-skills` opt-out. It preserves the approved bundled allowlist on
the default, coder, content, and ops homes while keeping design and research
empty through Hermes's existing per-skill `.curator_suppressed` files.

Before a policy mutation, `install_fleet_config.py` writes target-only skill
tarballs below the install receipt directory:

```text
~/.hermes/logs/fleet-config-installs/<stamp>/skill-policy-prechange/
```

Every removed directory is also moved intact, never deleted, below:

```text
~/.hermes/archives/fleet-skill-policy/
  hermes-mini-skill-surface-2026-08-01/<stamp>/<profile>/<kind>/
```

Before the standalone
`autonomous-ai-agents/clickup-queue-poller-merged-before-claim` skill is moved,
its two approved historical references are hash-verified and copied into
`clickup-queue-poller/references/_archive/`. The policy pins the source and
destination path plus the source SHA-256 for each file. The installer refuses
the whole run if a source has drifted, a destination has conflicting bytes, or
neither copy exists. A rollback removes newly copied archive references and
restores the standalone skill; a repeated successful install accepts the
already-consolidated, hash-identical destination without rewriting it.

The local `vehicle-image-qc` shadow is removable only when its hub lock entry
matches all three policy-pinned provenance fields: `install_path`, `source`,
and `identifier`. A same-name directory or same-path lock entry from any other
hub source fails closed.

Hermes skill sync may leave a suppressed bundled skill's category
`DESCRIPTION.md` behind even though its `SKILL.md` and scripts are no longer
active. The installer treats that path as already inactive only when it is a
real directory containing exactly that one regular file and its bytes match
the bundled source. Symlinks, extra files, nested manifests, missing source
metadata, and byte drift all fail closed.

The default home's obsolete `sentry-monitor` skill path is also archived. Its
`SKILL.md` may be a symlink into `~/.hermes/repos/ignite-sentinel`; moving the
wrapper directory preserves that link in the recoverable archive and does not
move or modify the operational Ignite Sentinel checkout or its launchd jobs.

An installer failure restores moved skills and metadata automatically. For a
later operator-directed recovery, copy the desired archived directory back to
its original relative path under that profile's `skills/` directory, remove
the skill's line from `.curator_suppressed` when restoring a bundled skill,
and run `hermes skills sync` for that profile. Restoring the local
`vehicle-image-qc` shadow is normally wrong: the canonical Ignite external
skill should remain the only resolver winner. Restoring the old
`sentry-monitor` wrapper is likewise unnecessary while Ignite Sentinel remains
operational through its governed repository and launchd paths.

Never recover this policy by adding `.no-bundled-skills`, copying the Ignite
tree into a profile, or deleting the governed archives.
