/**
 * status_guard.mjs — the single ClickUp completion chokepoint for Hermes.
 *
 * Pure, network-free guard functions enforcing the 2026-06-22 completion policy.
 * clickup.mjs (the autonomous executor's ONLY ClickUp client — Hermes has no
 * ClickUp MCP) routes every status write through checkG1/checkG2 and every
 * comment through checkG3 before touching the API, so the invariants can't be
 * bypassed per-call. The functions here do no I/O: callers fetch the task /
 * comments and pass them in, which keeps the rules unit-testable.
 *
 * Background (the failures this prevents — observed 2026-06-22, 106 tasks):
 *   G1  Hermes set tasks to `complete`, bypassing the ignite-validate QA gate.
 *   G2  Hermes closed tasks OVER a standing `ignite-validate: FAIL`.
 *   G3  Hermes wrote "Closed per Colin" with no actual Colin sign-off.
 * G4–G6 (objective-verified, partial=not-done, cited metrics) are content-level
 * and live in the SKILL prompt + the closeout_audit.mjs lint; the deterministic
 * chokepoint owns G1–G3.
 *
 * Why G3 hard-bans the phrase class instead of checking authorship: clickup.mjs
 * authenticates with Colin's personal token, so EVERY comment Hermes posts is
 * attributed in ClickUp to user 168143285 ("Colin Greig"). Author id therefore
 * cannot discriminate Hermes from Colin — the only safe rule is that Hermes
 * never asserts a human sign-off it cannot prove.
 */

// Colin's ClickUp user id. The only human whose authorization is genuine — but
// see the module header: Hermes posts AS this id, so it is not a discriminator.
export const COLIN_USER_ID = '168143285';

// G1: closed-type status names Hermes must never set. ClickUp lists vary on the
// exact label ("complete" vs "done" vs "closed"), so we match the class.
const COMPLETE_WORDS = ['complete', 'completed', 'closed', 'done', 'archived', 'archive'];

// Review-class: the terminal ceiling Hermes IS allowed (its hand-off to the
// validator), plus the names lists use for it.
const REVIEW_WORDS = [
  'ready for review', 'in review', 'review', 'for review',
  'qa', 'qa review', 'needs review', 'ready for qa', 'in qa',
];

// Negative validator verdicts that forbid Hermes from advancing the task.
const NEGATIVE_VERDICTS = /^(fail|failed|block|blocked|reject|rejected)$/i;

const norm = (s) => String(s ?? '').trim().toLowerCase();

export function isCompleteClass(statusName) {
  return COMPLETE_WORDS.includes(norm(statusName));
}

export function isReviewClass(statusName) {
  return REVIEW_WORDS.includes(norm(statusName));
}

/** An "advance" = moving toward done (review OR complete). G2 gates these. */
export function isAdvanceStatus(statusName) {
  return isReviewClass(statusName) || isCompleteClass(statusName);
}

// --- G1: Hermes never sets a complete-class status ------------------------

export function checkG1_noComplete(statusName, { allowComplete = false } = {}) {
  if (isCompleteClass(statusName) && !allowComplete) {
    return {
      ok: false,
      code: 'G1',
      message:
        `G1 BLOCKED: Hermes must NEVER set ClickUp status to a complete-class status ` +
        `("${statusName}"). Hermes's terminal status is "in review"; "complete" is owned ` +
        `exclusively by the ignite-validate QA gate / Colin (2026-06-22 policy). ` +
        `Park at "in review" and let the validator sign off. ` +
        `[genuine human/validator use only: set CLICKUP_ALLOW_COMPLETE=1]`,
    };
  }
  return { ok: true, code: 'G1' };
}

// --- G2: respect the validator's standing verdict -------------------------

/** Pull the comment body as a string, tolerating ClickUp's two shapes
 * (flat `comment_text` string, or structured `comment` segment array). */
export function commentText(c) {
  if (!c) return '';
  if (typeof c.comment_text === 'string' && c.comment_text) return c.comment_text;
  if (Array.isArray(c.comment)) return c.comment.map((seg) => seg?.text ?? '').join('');
  if (typeof c.comment === 'string') return c.comment;
  return '';
}

const VALIDATE_MARKER = /ignite-validate:\s*([a-z]+)/i;

/**
 * Find the most recent `ignite-validate:` marker across a task's comments.
 * Returns { verdict, date, commentId, text } or null. `date` is a Number (ms).
 * Robust to comment ordering: sorts by date descending before picking.
 */
export function latestValidateVerdict(comments) {
  const marked = (comments ?? [])
    .map((c) => {
      const text = commentText(c);
      const m = text.match(VALIDATE_MARKER);
      if (!m) return null;
      return {
        verdict: m[1].toLowerCase(),
        date: Number(c.date ?? c.date_created ?? 0) || 0,
        commentId: c.id ?? null,
        text,
      };
    })
    .filter(Boolean);
  if (!marked.length) return null;
  marked.sort((a, b) => b.date - a.date);
  return marked[0];
}

/**
 * G2 — do not advance (to review/complete) over a standing negative verdict.
 * Only a NEWER `ignite-validate: PASS` clears a FAIL/BLOCK; Hermes self-posting
 * a "shipped" closeout after the FAIL does NOT (that is the exact presswizz
 * loophole). To rework, Hermes re-picks from "to do" (not an advance, so not
 * gated) and lets the validator re-run.
 */
export function checkG2_respectVerdict(statusName, comments, { allowOverride = false } = {}) {
  if (!isAdvanceStatus(statusName)) return { ok: true, code: 'G2' };
  const v = latestValidateVerdict(comments);
  if (v && NEGATIVE_VERDICTS.test(v.verdict) && !allowOverride) {
    return {
      ok: false,
      code: 'G2',
      verdict: v,
      message:
        `G2 BLOCKED: the latest ignite-validate marker on this task is ` +
        `"${v.verdict.toUpperCase()}"${v.commentId ? ` (comment ${v.commentId})` : ''}. ` +
        `Hermes must NOT advance the task to "${statusName}" over a standing validator ` +
        `FAIL/BLOCK. Re-pick from "to do", redo the work, and let ignite-validate re-run — ` +
        `only a newer "ignite-validate: PASS" clears this. ` +
        `[validator/human override: set CLICKUP_ALLOW_FAIL_OVERRIDE=1]`,
    };
  }
  return { ok: true, code: 'G2' };
}

// --- G3: no fabricated human authorization --------------------------------

export const BANNED_AUTH_PATTERNS = [
  /\bper\s+colin\b/i,
  /\bcolin\s+(approved|accepted|signed?[\s-]*off|confirmed|authoriz\w*|ok'?d|okayed|gave\s+the\s+(go|ok|green))/i,
  /\b(approved|accepted|authoriz\w*|signed?[\s-]*off|confirmed|cleared|greenlit)\s+by\s+colin\b/i,
  /\bcolin'?s\s+(approval|sign[\s-]*off|go[\s-]*ahead|ok\b|blessing|authoriz\w*|instruction)/i,
  /\bper\s+(the\s+)?(operator|owner)(?:'?s)?\s+(approval|sign[\s-]*off|instruction|go[\s-]*ahead)/i,
  /\bhuman\s+(approval|sign[\s-]*off)\s+(received|obtained|granted|confirmed)/i,
  /\bas\s+(approved|authoriz\w*|instructed)\s+by\s+colin\b/i,
];

export function findBannedAuthClaims(text) {
  if (!text) return [];
  const hits = [];
  for (const re of BANNED_AUTH_PATTERNS) {
    const m = String(text).match(re);
    if (m) hits.push(m[0].trim());
  }
  return hits;
}

export function checkG3_noFabricatedAuth(text, { allowAuthClaim = false } = {}) {
  const hits = findBannedAuthClaims(text);
  if (hits.length && !allowAuthClaim) {
    return {
      ok: false,
      code: 'G3',
      hits,
      message:
        `G3 BLOCKED: this comment asserts human authorization (matched: ` +
        `${hits.map((h) => `"${h}"`).join(', ')}). Hermes posts via Colin's PAT and cannot ` +
        `prove a human sign-off, so it must never fabricate one (e.g. "Closed per Colin"). ` +
        `Remove the authorization claim — if a human genuinely authorized this, that stands ` +
        `as Colin's own comment and Hermes does not restate it. ` +
        `[genuine human use only: set CLICKUP_ALLOW_AUTH_CLAIM=1]`,
    };
  }
  return { ok: true, code: 'G3' };
}

// --- G6 / G4 helpers (used by closeout_audit.mjs; advisory, not blocking) ---

// A closeout that CLAIMS the objective is done.
export const DONE_CLAIM_PATTERN =
  /\b(shipped|deployed|verified\s+live|live[\s-]*verified|is\s+(now\s+)?live|done\s+(and|\+)|completed?|✅|delivered)\b/i;

// Positive runtime/deploy/CI evidence a done-claim should cite (G4/G6).
export const RUNTIME_EVIDENCE_PATTERN = new RegExp(
  [
    'https?://',                       // a live URL / deployment / dashboard
    'wrangler\\s+deploy',              // CF Worker actually deployed
    '\\bgh\\s+run\\b', 'actions/runs', // a CI run reference
    '\\brun\\s+#?\\d+', 'workflow\\s+run',
    '\\b[0-9a-f]{7,40}\\b',            // a commit SHA
    'curl\\s', 'HTTP/\\d', '\\b200\\b\\s+OK',
    'rows?\\s*=\\s*\\d+', 'count\\s*(now|=|:)\\s*\\d+', '\\d+\\s*/\\s*\\d+\\s+(delivered|records|rows)',
    'deployment\\s+id', 'preview\\s+url',
  ].join('|'),
  'i',
);

/**
 * Heuristic G4/G6 check for the audit: a comment that claims "done" but cites no
 * positive runtime evidence. Returns true if it looks like a bare done-claim.
 */
export function isUncitedDoneClaim(text) {
  if (!text) return false;
  return DONE_CLAIM_PATTERN.test(text) && !RUNTIME_EVIDENCE_PATTERN.test(text);
}
