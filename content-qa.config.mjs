// content-qa.config.mjs — ALWAYS at the git top level. Every path below is
// GIT-TOP-RELATIVE (see skills/ignite-state/references/content-qa.md in the
// ignite plugin — "Path discipline"). Loaded only inside the engine's
// sandboxed worker.
//
// hermes-agent's npm project lives at the git top level, so `subdir` is
// omitted and every governed path is spelled out in full.
//
// Governed content is internal Markdown deliverables only — team changelogs,
// announcements, and other operator-facing docs under docs/internal/. General
// engineering runbooks (README.md, AGENTS.md, machine-setup/**, docs/plans/**,
// etc.) are deliberately excluded: they are not content-lane deliverables and
// should not carry cosmetic frontmatter purely to satisfy this gate.
export default {
  contract: 'content-qa/v1',
  policy: './scripts/content-qa.policy.mjs',
  contentGlobs: [
    // Real governed internal Markdown deliverables.
    'docs/internal/**/*.md',
    // policyTripwires fixtures live here — see note below.
    'docs/internal/content-qa-fixtures/**/*.md',
  ],
  policyTripwires: [
    {
      file: 'docs/internal/content-qa-fixtures/missing-doc-type.md',
      expectFailingCheckId: 'hermes.internal-doc-type',
    },
  ],
};
