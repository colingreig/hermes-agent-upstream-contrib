---
title: "Internal Markdown deliverables (content-qa/v1)"
date: 2026-07-30
doc_type: internal-guide
---

# Internal Markdown deliverables (content-qa/v1)

This directory holds **governed internal Markdown deliverables** for hermes-agent.
Files here are certified by the repository's `content-qa/v1` policy engine
(`content-qa.config.mjs` at the git top level, policy in
`scripts/content-qa.policy.mjs`).

Engineering runbooks, AGENTS.md, machine-setup docs, and planning snapshots
elsewhere in the repository are **not** governed by content QA. Only paths
matching `docs/internal/**/*.md` enter the gate.

## Governed paths

| Path pattern | Purpose |
| --- | --- |
| `docs/internal/changelog/*.md` | Internal fleet/operator changelogs and announcements |
| `docs/internal/content-qa-fixtures/**/*.md` | Committed policy tripwire fixtures (probe conformance only) |

## Fleet changelog convention

Fleet rebuild and operator-facing changelogs use:

```
docs/internal/changelog/<YYYY-MM-DD>-<slug>.md
```

Frontmatter:

```yaml
---
title: "<Human-readable title>"
date: <YYYY-MM-DD>
doc_type: fleet-changelog
---
```

Mechanical policy checks for `doc_type: fleet-changelog` (and files under
`docs/internal/changelog/`):

- Body word count: **400–700 words**
- Structure: one H1 title plus at least one H2 section
- Plus toolkit baseline checks (valid frontmatter, no placeholders, non-empty sections)

## 2026-07-29 Hermes fleet rebuild changelog

The internal changelog for the 2026-07-29 Hermes fleet gut-and-rebuild
(ClickUp task `86e2jep4g`) must be written to:

```
docs/internal/changelog/2026-07-29-hermes-fleet-rebuild.md
```

Source material: `machine-setup/fleet-config/README.md` and
`machine-setup/fleet-config/CUTOVER.md` (merged PRs #210, #211, #212).

After the Markdown file is committed at that path, run exact content QA:

```bash
node "$CONTENT_QA_ENGINE" --root . --mode exact \
  --path docs/internal/changelog/2026-07-29-hermes-fleet-rebuild.md \
  --report "$CONTENT_QA_REPORT"
```

The finished changelog may also be posted to the ClickUp task comment for
team visibility; the **repository path above is the governed deliverable**
that content QA certifies.

## Probe / enablement validation

Repository enablement is verified with:

```bash
node /Users/colingreig/.codex/skills/ignite-state/scripts/content-qa-command.mjs \
  --root . --mode probe
```

Expect exit code 0 when the config, policy, and tripwire fixtures are correct.
