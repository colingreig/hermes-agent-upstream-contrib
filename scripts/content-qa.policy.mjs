// scripts/content-qa.policy.mjs — the content-qa/v1 PURE policy for
// hermes-agent: (content, ctx) => findings[]. Runs inside the toolkit
// engine's sandboxed worker (see skills/ignite-state/references/content-qa.md
// in the ignite plugin) — no fs, no child_process, no network, no ambient
// env. It receives exactly `content` (`{ path, bytes }[]`, bytes already
// UTF-8 decoded) and `ctx` (`contract`, `subdir`, `publicDirs`,
// `assetPaths`, `checkedPaths`, `frontmatter`).
//
// Mechanical coverage only: path scope, frontmatter shape, merge-conflict
// markers, fleet-changelog word bounds, and minimal structural headings.
// Editorial judgment stays with the content lane and ignite-validate.
//
// Toolkit baseline floor (frontmatter-valid, no-placeholder-markers,
// no-broken-local-assets, no-empty-sections) already runs unconditionally,
// in the trusted parent, for every *.md path.

const INTERNAL_PATH = /^docs\/internal\/(?:[^/]+\/)*[^/]+\.md$/;
const CHANGELOG_PATH = /^docs\/internal\/changelog\/[^/]+\.md$/;
const FIXTURE_PATH = /^docs\/internal\/content-qa-fixtures\/[^/]+\.md$/;

const CONFLICT_MARKER_PATTERN = /^(?:<{7}|={7}|>{7})(?: .*)?$/m;
const FRONTMATTER_BLOCK_PATTERN = /^(?:\uFEFF)?---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/;

const FLEET_CHANGELOG_MIN_WORDS = 400;
const FLEET_CHANGELOG_MAX_WORDS = 700;

function finding(id, path, message) {
  return message === undefined
    ? { id, pass: true, path }
    : { id, pass: false, path, message };
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.trim() !== '';
}

function validDateValue(value) {
  return value instanceof Date
    ? !Number.isNaN(value.getTime())
    : (typeof value === 'string' || typeof value === 'number')
      && !Number.isNaN(Date.parse(String(value)));
}

function splitFrontmatterBody(bytes) {
  const match = bytes.match(FRONTMATTER_BLOCK_PATTERN);
  return match ? bytes.slice(match[0].length) : bytes;
}

function countWords(text) {
  const trimmed = text.trim();
  if (!trimmed) return 0;
  return trimmed.split(/\s+/).length;
}

function kindFor(path) {
  if (FIXTURE_PATH.test(path)) return 'fixture';
  if (CHANGELOG_PATH.test(path)) return 'fleet-changelog';
  if (INTERNAL_PATH.test(path)) return 'internal';
  return null;
}

function checkConflictMarkers(file, out) {
  if (CONFLICT_MARKER_PATTERN.test(file.bytes)) {
    out.push(
      finding(
        'hermes.conflict-markers',
        file.path,
        'source contains unresolved merge conflict markers',
      ),
    );
  } else {
    out.push(finding('hermes.conflict-markers', file.path));
  }
}

function checkInternalDocType(file, ctx, out) {
  const parsed = ctx.frontmatter?.[file.path];
  if (!parsed || !parsed.ok) {
    return;
  }
  const docType = parsed.data?.doc_type;
  if (isNonEmptyString(docType)) {
    out.push(finding('hermes.internal-doc-type', file.path));
  } else {
    out.push(
      finding(
        'hermes.internal-doc-type',
        file.path,
        'internal Markdown deliverables require frontmatter doc_type',
      ),
    );
  }
}

function checkInternalFrontmatter(file, ctx, out) {
  const parsed = ctx.frontmatter?.[file.path];
  if (!parsed || !parsed.ok) {
    return;
  }
  const problems = [];
  if (!isNonEmptyString(parsed.data?.title)) {
    problems.push('title must be a non-empty string');
  }
  if (!validDateValue(parsed.data?.date)) {
    problems.push('date must be a valid date');
  }
  if (problems.length > 0) {
    out.push(
      finding('hermes.internal-frontmatter', file.path, problems.join('; ')),
    );
  } else {
    out.push(finding('hermes.internal-frontmatter', file.path));
  }
}

function checkBodyNonEmpty(file, out) {
  const body = splitFrontmatterBody(file.bytes);
  if (/\S/.test(body)) {
    out.push(finding('hermes.body-non-empty', file.path));
  } else {
    out.push(
      finding('hermes.body-non-empty', file.path, 'Markdown body must not be empty'),
    );
  }
}

function isFleetChangelog(file, ctx) {
  const kind = kindFor(file.path);
  if (kind === 'fleet-changelog') return true;
  const parsed = ctx.frontmatter?.[file.path];
  return parsed?.ok && parsed.data?.doc_type === 'fleet-changelog';
}

function checkFleetChangelogWordCount(file, ctx, out) {
  if (!isFleetChangelog(file, ctx)) {
    return;
  }
  const body = splitFrontmatterBody(file.bytes);
  const words = countWords(body);
  if (words >= FLEET_CHANGELOG_MIN_WORDS && words <= FLEET_CHANGELOG_MAX_WORDS) {
    out.push(finding('hermes.fleet-changelog-word-count', file.path));
    return;
  }
  out.push(
    finding(
      'hermes.fleet-changelog-word-count',
      file.path,
      `fleet changelog body must be ${FLEET_CHANGELOG_MIN_WORDS}-${FLEET_CHANGELOG_MAX_WORDS} words (found ${words})`,
    ),
  );
}

function checkFleetChangelogStructure(file, ctx, out) {
  if (!isFleetChangelog(file, ctx)) {
    return;
  }
  const body = splitFrontmatterBody(file.bytes);
  const hasH1 = /^#\s+\S/m.test(body);
  const hasSection = /^##\s+\S/m.test(body);
  if (hasH1 && hasSection) {
    out.push(finding('hermes.fleet-changelog-structure', file.path));
    return;
  }
  const problems = [];
  if (!hasH1) problems.push('body must include an H1 heading');
  if (!hasSection) problems.push('body must include at least one H2 section');
  out.push(
    finding(
      'hermes.fleet-changelog-structure',
      file.path,
      problems.join('; '),
    ),
  );
}

export function policy(content, ctx) {
  const findings = [];
  for (const file of content) {
    const kind = kindFor(file.path);
    if (!kind) {
      findings.push(
        finding(
          'hermes.content-scope',
          file.path,
          'path is not a recognized internal Markdown deliverable (docs/internal/**/*.md)',
        ),
      );
      continue;
    }

    checkConflictMarkers(file, findings);
    checkInternalDocType(file, ctx, findings);
    checkInternalFrontmatter(file, ctx, findings);
    checkBodyNonEmpty(file, findings);
    checkFleetChangelogWordCount(file, ctx, findings);
    checkFleetChangelogStructure(file, ctx, findings);
  }
  return findings;
}

export default policy;
