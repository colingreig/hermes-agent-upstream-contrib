---
title: "Content QA Policy Tripwire Fixture"
date: 2026-07-30
---

# Policy Tripwire Fixture

This file is a committed `policyTripwires` fixture for the `content-qa/v1`
policy (see `content-qa.config.mjs`). It exists to prove the repository
policy actually fires, not merely that it loads — a do-nothing policy must
fail the engine probe, exactly as the toolkit baseline cannot certify
itself.

The frontmatter above deliberately omits `doc_type`, which must fail the
`hermes.internal-doc-type` check under the shared engine probe.
