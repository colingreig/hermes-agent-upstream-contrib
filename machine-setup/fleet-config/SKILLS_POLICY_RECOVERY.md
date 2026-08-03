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
  hermes-mini-skill-surface-2026-08-03/<stamp>/<profile>/<kind>/
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

## Self-authored and other ungoverned skills

Hermes writes skills for itself from the background self-improvement review
fork. Anything that lands in an installed `skills/` tree without being
classified by `skills-policy.json` — self-authored skills, hand-copied skills,
a hub install nobody pinned — is **ungoverned**.

`ungoverned_active.mode` decides what the installer does with them:

- `quarantine` (shipped): each ungoverned skill root is archived exactly like
  a policy removal — pre-change tarball, whole-directory move under
  `<stamp>/<profile>/ungoverned/<rel>`, move-back rollback, and a receipt line
  carrying its recorded disposition. The install then continues.
- `fail`: refuse the whole install (the pre-2026-08-03 behaviour). An omitted
  `ungoverned_active` section also means `fail`, so an older policy file can
  never be silently relaxed by a newer installer.

A `SKILL.md` found *inside* a governed skill directory is that skill's own
vendored reference content and collapses onto its owning skill; it is neither
counted as a separate active skill nor quarantined.

`ungoverned_active.dispositions` is the audit trail of what an operator decided
about a specific skill (`discard` or `promotion-pending`, each with a written
reason). It is advisory: an unlisted skill is still quarantined, and simply
reports as `unreviewed` in the plan and the receipt.

### Promoting a self-authored skill into the fleet

1. Recover its directory from the quarantine archive back to its original
   relative path under the profile's `skills/` directory.
2. Add it to `required_local_keep` in `skills-policy.json` and drop or update
   its `dispositions` entry.
3. Bump `profiles.default.expected_active_manifests` to match — the loader
   derives the expected count from `bundled.keep` plus `required_local_keep`
   and refuses a policy whose declared number disagrees.
4. Regenerate the `skills-policy.json` sha256 in `fleet_config_manifest.json`.
   Skipping this silently aborts and rolls back every cutover.

Do step 1 before shipping steps 2-3: `required_local_keep` fails closed when
the skill is missing from the live tree.

### Standing disposition: `promise-validation` — discard

Created by the self-improvement review on 2026-08-02 at
`software-development/promise-validation`, it made the default profile 25
active manifests against an expected 24 and hard-blocked `install_fleet_config.py`
on 2026-08-03 (task 86e2kxk52). It is **discarded, not promoted**: its guidance
(validate the running artefact, not the diff) is already the governed contract
carried by `ignite-validate` under `IGNITE_SKILLS_ROOT`, plus the bundled
`test-driven-development` and `systematic-debugging` keeps. The bytes remain
recoverable under the quarantine archive and the manual
`~/.hermes/skills-quarantine/` stopgap; that stopgap directory is inert (it is
outside every `skills/` tree) and can be swept whenever convenient.

Never recover this policy by adding `.no-bundled-skills`, copying the Ignite
tree into a profile, or deleting the governed archives.
