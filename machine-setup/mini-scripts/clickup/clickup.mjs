#!/usr/bin/env node
/**
 * clickup.mjs — a light, portable ClickUp REST API v2 CLI.
 *
 * Self-contained: plain Node ESM (Node 18+ for built-in fetch), no build step,
 * no tsx, no project dependencies. Runs in any project / any directory.
 *
 * A standalone alternative to the ClickUp MCP server for the operations the
 * agency workflow actually uses: read/list tasks, create tasks, comment, and
 * move status (incl. the "Ready for Review" / "In Progress" hand-off pattern).
 *
 * Auth: resolves CLICKUP_API_TOKEN in two steps — (1) the ambient environment
 * (if exported or running under `op-run`), else (2) 1Password directly
 * (op://Dev Toolbox/dev/CLICKUP_API_TOKEN, via the read-only service-account
 * token at ~/.config/op-runtime-token). The 1Password fallback means no
 * --env-file and no settings.json env entry are needed. Personal tokens (pk_…)
 * go in the Authorization header with NO "Bearer" prefix.
 *
 * Names → IDs: a co-located references/ids.json maps friendly names to IDs, so
 * you can write `--list fieldboss:seo` and `--assignee colin` instead of digits.
 *
 *   node clickup.mjs whoami
 *   node clickup.mjs create --list fieldboss:seo --name "Fix canonical tags" --priority high
 *   node clickup.mjs review 86abc123 "Shipped — see PR #482"
 *
 * The v2 rate limit is 100 req/min/token; on HTTP 429 this CLI waits for the
 * X-RateLimit-Reset epoch and retries (up to 3×) rather than failing.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import {
  checkG1_noComplete,
  checkG2_respectVerdict,
  checkG3_noFabricatedAuth,
  isAdvanceStatus,
} from './status_guard.mjs';
import {
  getCached, setCached, markWrite, hashKey, clearCache, cacheDir, cacheStats,
} from './cache.mjs';
import {
  upsertTask, patchTask, removeTask, syncFromBoard, findTasks, indexStats,
} from './taskindex.mjs';

const API = 'https://api.clickup.com/api/v2';

// --- cache TTLs ------------------------------------------------------------
// List metadata (status definitions) is near-static — a task moving never
// changes it — so it caches long and ignores the write-epoch. Board task pulls
// are volatile: short TTL AND evicted by any local write (see cache.mjs).
const META_TTL_MS = 7 * 24 * 60 * 60 * 1000;     // 7d — status defs are near-static
const BOARD_TTL_MS = 60 * 1000;                  // 60s (override per-call with --max-age)

// --- Hermes completion guardrails (2026-06-22 policy) ----------------------
// Every status write and every comment funnels through these so G1/G2/G3 can't
// be bypassed per-call. Overrides are env-gated for genuine human/validator use;
// the autonomous executor's environment never sets them. See status_guard.mjs.
function envFlag(name) {
  const v = process.env[name];
  return v === '1' || /^(true|yes|on)$/i.test(v ?? '');
}

/** Guard a status transition. Fetches comments only when actually advancing. */
async function guardStatusOrFail(taskId, statusName) {
  const g1 = checkG1_noComplete(statusName, { allowComplete: envFlag('CLICKUP_ALLOW_COMPLETE') });
  if (!g1.ok) fail(g1.message);
  if (isAdvanceStatus(statusName)) {
    const { comments } = await cu(`/task/${taskId}/comment`);
    const g2 = checkG2_respectVerdict(statusName, comments ?? [], {
      allowOverride: envFlag('CLICKUP_ALLOW_FAIL_OVERRIDE'),
    });
    if (!g2.ok) fail(g2.message);
  }
}

/** Guard a comment body (G3 — no fabricated human authorization). */
function guardCommentOrFail(text) {
  const g3 = checkG3_noFabricatedAuth(text, { allowAuthClaim: envFlag('CLICKUP_ALLOW_AUTH_CLAIM') });
  if (!g3.ok) fail(g3.message);
}

// critical→1(urgent), high→2, medium/normal→3, low→4
const PRIORITY = {
  urgent: 1, critical: 1, '1': 1,
  high: 2, '2': 2,
  normal: 3, medium: 3, '3': 3,
  low: 4, '4': 4,
};

// --- id/name mapping (co-located, optional) --------------------------------

let IDS = null;
function ids() {
  if (IDS) return IDS;
  try {
    IDS = JSON.parse(readFileSync(new URL('./references/ids.json', import.meta.url), 'utf8'));
  } catch {
    IDS = { workspace: {}, people: {}, spaces: {}, clients: {}, lists: {} };
  }
  return IDS;
}

/** Resolve a --list value: raw numeric id | client:listkey | flat alias. */
function resolveList(v) {
  if (/^\d+$/.test(v)) return v;
  const m = ids();
  if (v.includes(':')) {
    const [client, key] = v.split(':');
    const id = m.clients?.[client]?.lists?.[key];
    if (id) return id;
  }
  if (m.lists?.[v]) return m.lists[v];
  if (m.clients?.[v]?.lists?.main) return m.clients[v].lists.main;
  const clientKeys = Object.keys(m.clients ?? {});
  fail(`could not resolve list "${v}". Use a numeric id, a flat alias (node clickup.mjs ids lists), or client:listkey — clients: ${clientKeys.join(', ')}`);
}

/** Resolve a person value to a numeric user id: raw id | name alias. */
function resolvePerson(v) {
  if (/^\d+$/.test(v)) return v;
  const p = ids().people?.[v.toLowerCase()];
  if (p?.id) return String(p.id);
  fail(`unknown person "${v}". Known: ${Object.keys(ids().people ?? {}).join(', ')}`);
}

// --- auth + fetch ----------------------------------------------------------

function token() {
  // 1) ambient env (fast path: already exported, or running under `op-run`).
  let t = process.env.CLICKUP_API_TOKEN;
  // 2) fall back to 1Password (master secret store: op://Dev Toolbox/dev).
  if (!t) t = fromOnePassword();
  if (!t) {
    fail(
      'CLICKUP_API_TOKEN not found. Either export it, run under `op-run`, ' +
      'or ensure 1Password has it and the SDK resolver can read ' +
      'op://Dev Toolbox/dev/CLICKUP_API_TOKEN',
    );
  }
  return t.replace(/^Bearer\s+/i, '').trim();
}

/** Pull the token from 1Password via the SDK resolver. Returns null on any failure. */
function fromOnePassword() {
  const tokenPath = process.env.OP_RUNTIME_TOKEN || `${process.env.HOME}/.config/op-runtime-token`;
  try {
    if (!readFileSync(tokenPath, 'utf8').trim()) return null;
  } catch {
    return null;
  }
  try {
    const py = `${process.env.HOME}/.hermes/runtime-current/venv/bin/python`;
    const scriptDir = `${process.env.HOME}/.hermes/scripts`;
    const code = [
      'import sys',
      'sys.path.insert(0, sys.argv[1])',
      'from op_sdk_resolve import resolve_refs',
      'ref = sys.argv[2]',
      'vals = resolve_refs([ref])',
      "sys.stdout.write(vals.get(ref, ''))",
    ].join('; ');
    const out = execFileSync(
      py,
      ['-c', code, scriptDir, 'op://Dev Toolbox/dev/CLICKUP_API_TOKEN'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
    ).trim();
    return out || null;
  } catch {
    return null;
  }
}

function fail(msg) {
  console.error(`✗ ${msg}`);
  process.exit(1);
}

/** fetch wrapper with 429-aware retry against X-RateLimit-Reset. */
async function cu(path, init = {}, attempt = 0) {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: { Authorization: token(), 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  });

  if (res.status === 429 && attempt < 3) {
    const reset = Number(res.headers.get('x-ratelimit-reset'));
    const waitMs = reset ? Math.min(Math.max(reset * 1000 - Date.now(), 1000), 60_000) : 2000 * (attempt + 1);
    console.error(`  rate-limited (429); waiting ${Math.round(waitMs / 1000)}s then retrying…`);
    await new Promise((r) => setTimeout(r, waitMs));
    return cu(path, init, attempt + 1);
  }

  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    if (init.soft) return { ok: false, status: res.status, body };
    fail(`ClickUp ${init.method ?? 'GET'} ${path} → ${res.status}: ${text}`);
  }
  return init.soft ? { ok: true, status: res.status, body } : body;
}

/**
 * Fetch a list's metadata (name, statuses, …) through the static-metadata cache.
 * Status definitions change ~never during a work session, so this caches for
 * META_TTL_MS and ignores the write-epoch (a task write must not evict it).
 * Callers that resolve create/handoff statuses hit this on every write, so the
 * cache turns an N-writes-per-cycle tax into a single fetch. `--fresh` bypasses.
 */
async function listMeta(listId, { fresh = false } = {}) {
  const key = `list-meta-${listId}`;
  if (!fresh) {
    const hit = getCached(key, { maxAgeMs: META_TTL_MS, ignoreEpoch: true });
    if (hit) return hit.data;
  }
  const list = await cu(`/list/${listId}`);
  setCached(key, list);
  return list;
}

// --- arg parsing -----------------------------------------------------------

function parseArgs(argv) {
  const _ = [];
  const flags = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) flags[key] = true;
      else { flags[key] = next; i++; }
    } else _.push(a);
  }
  return { _, flags };
}

const json = (v) => JSON.stringify(v, null, 2);

// --- --out <path>: write a task-list JSON dump to disk instead of stdout ----
// Multi-task pulls (cmdList) can run into the hundreds of tasks; printing that
// straight to stdout means it transits the calling agent's context in full on
// every board pull. --out writes the array to a file and prints a one-line
// summary (path + count) instead, so callers grep/jq the file locally rather
// than reading the whole dump inline. Falls back to normal stdout JSON when
// --out is absent — existing callers are unaffected.
function writeJsonOut(tasks, outPath) {
  writeFileSync(outPath, json(tasks));
  console.log(`✓ wrote ${tasks.length} task${tasks.length === 1 ? '' : 's'} to ${outPath}`);
}

// --- --compact projection ---------------------------------------------------
// Strips heavy, triage-irrelevant fields off a raw ClickUp task object while
// preserving object shape (nested objects/arrays stay as-is) so existing skill
// parsers (t.status.status, t.tags[].name, etc.) keep working unchanged.
const HEAVY_TASK_KEYS = ['custom_fields', 'description', 'text_content', 'markdown_description', 'attachments', 'checklists'];
const compactTask = (t) => {
  if (!t || typeof t !== 'object') return t;
  const rest = { ...t };
  for (const k of HEAVY_TASK_KEYS) delete rest[k];
  return rest;
};

// --- commands --------------------------------------------------------------

async function cmdWhoami() {
  const { user } = await cu('/user');
  console.log(`✓ ${user.username} <${user.email}>  (id ${user.id})`);
}

async function cmdTeams() {
  const { teams } = await cu('/team');
  for (const t of teams) console.log(`${t.id}\t${t.name}`);
}

async function cmdSpaces(teamId) {
  let id = teamId ? resolveSpaceTeam(teamId) : ids().workspace?.id;
  if (!id) { const { teams } = await cu('/team'); id = teams[0]?.id; }
  const { spaces } = await cu(`/team/${id}/space`);
  for (const s of spaces) console.log(`${s.id}\t${s.name}`);
}
function resolveSpaceTeam(v) { return /^\d+$/.test(v) ? v : (ids().workspace?.id ?? v); }

async function cmdFolders(spaceId) {
  const id = /^\d+$/.test(spaceId) ? spaceId : ids().spaces?.[spaceId.toLowerCase()];
  if (!id) fail(`unknown space "${spaceId}". Known: ${Object.keys(ids().spaces ?? {}).join(', ')}`);
  const { folders } = await cu(`/space/${id}/folder`);
  for (const f of folders) console.log(`${f.id}\t${f.name}`);
}

async function cmdLists(folderId) {
  const { lists } = await cu(`/folder/${folderId}/list`);
  for (const l of lists) console.log(`${l.id}\t${l.name}`);
}

async function cmdList(listRef, flags) {
  const listId = resolveList(listRef);
  const p = new URLSearchParams();
  if (flags['include-closed']) p.set('include_closed', 'true');
  if (typeof flags.status === 'string') p.append('statuses[]', flags.status);
  if (flags.assignee) {
    const v = flags.assignee === true ? 'me' : String(flags.assignee);
    const id = v === 'me' ? String((await cu('/user')).user.id) : resolvePerson(v);
    p.append('assignees[]', id);
  }
  // --subtasks flattens child tasks into the result alongside their parents. By
  // default the v2 list endpoint omits subtasks entirely, so any work decomposed
  // into children is invisible. Each subtask carries a non-null `parent` field.
  if (flags.subtasks) p.set('subtasks', 'true');
  const qs = p.toString();

  // Board task pulls are cached briefly (BOARD_TTL_MS) AND evicted by any local
  // write (write-epoch, see cache.mjs) — so a hit means "no write happened since
  // this snapshot AND it's within TTL". --fresh forces a live pull; --max-age N
  // overrides the TTL (seconds); CLICKUP_NO_CACHE=1 disables caching entirely.
  const cacheKey = `board-${listId}-${hashKey(qs)}`;
  const maxAgeMs = typeof flags['max-age'] === 'string'
    ? Math.max(0, Number(flags['max-age']) * 1000)
    : BOARD_TTL_MS;
  let tasks;
  if (!flags.fresh) {
    const hit = getCached(cacheKey, { maxAgeMs });
    if (hit) {
      tasks = hit.data;
      console.error(`  (cache hit — board pulled ${Math.round(hit.ageMs / 1000)}s ago; --fresh to bypass)`);
    }
  }
  if (!tasks) {
    ({ tasks } = await cu(`/list/${listId}/task?${qs}`));
    setCached(cacheKey, tasks);
  }
  // Keep the persistent task index in sync. An UNFILTERED pull is authoritative
  // (replace → catches externally added/removed tasks); a filtered pull only
  // upserts what it returned. `find` then resolves task ids locally, no API.
  const unfiltered = !flags.status && !flags.assignee;
  syncFromBoard(listId, tasks, { replace: unfiltered });
  const projected = flags.compact ? tasks.map(compactTask) : tasks;
  if (typeof flags.out === 'string') return writeJsonOut(projected, flags.out);
  if (flags.json) return console.log(json(projected));
  if (!tasks.length) return console.log('(no tasks)');
  for (const t of tasks) console.log(`${t.id}\t[${t.status?.status ?? '?'}]\t${t.parent ? '↳ ' : ''}${t.name}`);
}

/**
 * Find tasks by id or name substring in the LOCAL task index — no API call.
 * The index is populated by `create` (immediately) and by every `list` pull,
 * so a task made in one session is findable in another without pulling the
 * whole board. Pass --list to scope to one board, --sync to refresh from the
 * API first (falls through to a live `list` pull), --json for raw entries.
 */
async function cmdFind(query, flags) {
  const listId = typeof flags.list === 'string' ? resolveList(flags.list) : null;
  if (flags.sync) {
    if (!listId) fail('find --sync requires --list <id|client:key> to know which board to refresh');
    const { tasks } = await cu(`/list/${listId}/task?`);
    syncFromBoard(listId, tasks, { replace: true });
  }
  const matches = findTasks(query ?? '', { listId });
  if (flags.json) return console.log(json(matches));
  if (!matches.length) {
    return console.log(`(no local matches${query ? ` for "${query}"` : ''}) — run \`list <board>\` or \`find --list <board> --sync\` to populate/refresh the index`);
  }
  for (const e of matches) console.log(`${e.id}\t[${e.status ?? '?'}]\t${e.parent ? '↳ ' : ''}${e.name}\t${e.listName ? `(${e.listName})` : ''}`);
}

async function cmdTask(taskId, flags) {
  // --subtasks asks the v2 single-task endpoint to attach the full child-task
  // objects (each with its own status) as a `subtasks` array. Without it the
  // endpoint returns NO subtask data at all, so a parent can look "done" while
  // children are still open. include_subtasks returns children regardless of
  // their status (unlike the list endpoint's status filter).
  const t = await cu(`/task/${taskId}${flags.subtasks ? '?include_subtasks=true' : ''}`);
  if (flags.json) {
    if (!flags.compact) return console.log(json(t));
    const ct = compactTask(t);
    if (Array.isArray(ct.subtasks)) ct.subtasks = ct.subtasks.map(compactTask);
    return console.log(json(ct));
  }
  console.log(`${t.name}`);
  console.log(`  id:        ${t.id}`);
  console.log(`  status:    ${t.status?.status ?? '?'}`);
  console.log(`  priority:  ${t.priority?.priority ?? 'none'}`);
  console.log(`  assignees: ${(t.assignees ?? []).map((a) => a.username).join(', ') || 'none'}`);
  console.log(`  url:       ${t.url}`);
  const subs = t.subtasks ?? [];
  if (subs.length) {
    console.log(`  subtasks:  ${subs.length}`);
    for (const s of subs) console.log(`    - ${s.id}\t[${s.status?.status ?? '?'}]\t${s.name}`);
  }
  if (t.description) console.log(`\n${t.description}`);
}

/**
 * Choose the status a NEW task should land in when --status is omitted.
 * Standing rule (Colin): skills must never create tasks in a backlog / holding
 * status — always an active "to do". ClickUp otherwise defaults a new task to the
 * list's FIRST status, which on several lists (e.g. the Delivery client lists) is
 * "deferred" — that is how a blog-audit run once dumped 100+ tasks into deferred.
 * Returns a status name to use, or null to fall back to the ClickUp default.
 */
async function resolveCreateStatus(listId) {
  const list = await listMeta(listId);
  const raw = (list.statuses ?? []).filter((s) => s.status);
  const statuses = raw.map((s) => s.status);
  const lc = statuses.map((s) => s.toLowerCase());
  const HOLD = /(deferred|backlog|icebox|someday|on[\s-]?hold|blocked|parked)/;
  const pick = (...preds) => {
    for (const pred of preds) {
      const i = lc.findIndex(pred);
      if (i !== -1) return statuses[i];
    }
    return null;
  };
  return pick(
    (s) => s === 'to do',
    (s) => s === 'to-do' || s === 'todo',
    (s) => s === 'open',
    (s) => s === 'not started',
    (s) => s.includes('to do'),
    // else the first genuinely-active status (skip done/closed and any hold state)
    (s, i) => raw[i].type === 'open' && !HOLD.test(s),
    (s, i) => raw[i].type !== 'done' && raw[i].type !== 'closed' && !HOLD.test(s),
  );
}

async function cmdCreate(flags) {
  if (typeof flags.list !== 'string') fail('create requires --list <listId|client:listkey>');
  if (typeof flags.name !== 'string') fail('create requires --name "<task name>"');
  const listId = resolveList(flags.list);

  const body = { name: flags.name };
  if (typeof flags['desc-file'] === 'string') body.markdown_description = readFileSync(flags['desc-file'], 'utf8');
  else if (typeof flags.desc === 'string') body.markdown_description = flags.desc;
  if (typeof flags.status === 'string') {
    const g1 = checkG1_noComplete(flags.status, { allowComplete: envFlag('CLICKUP_ALLOW_COMPLETE') });
    if (!g1.ok) fail(g1.message);
    body.status = flags.status;
  } else if (flags['no-default-status'] === undefined) {
    // No explicit status → force an active "to do" rather than the list's
    // first status (often a backlog/"deferred" state). Pass --no-default-status
    // to opt out and let ClickUp pick the list default. (resolveCreateStatus
    // returns an open status, never complete-class, so no G1 check needed.)
    const s = await resolveCreateStatus(listId);
    if (s) body.status = s;
  }
  if (typeof flags.priority === 'string') {
    const pr = PRIORITY[flags.priority.toLowerCase()];
    if (!pr) fail(`unknown priority "${flags.priority}" (use urgent|high|normal|low)`);
    body.priority = pr;
  }
  if (typeof flags.tags === 'string') body.tags = flags.tags.split(',').map((s) => s.trim());
  if (typeof flags.parent === 'string') body.parent = flags.parent;
  if (typeof flags.assignee === 'string') body.assignees = [Number(resolvePerson(flags.assignee))];
  if (typeof flags.due === 'string') {
    const ms = Date.parse(flags.due);
    if (Number.isNaN(ms)) fail(`could not parse --due "${flags.due}" (try YYYY-MM-DD)`);
    body.due_date = ms;
  }

  const t = await cu(`/list/${listId}/task`, { method: 'POST', body: JSON.stringify(body) });
  markWrite();
  // The created task's id is known here — record it so a later session finds it
  // by name locally instead of pulling + looping the whole board. (list.id isn't
  // always on the create response, so stamp the list we created into.)
  upsertTask(listId, { ...t, list: t.list ?? { id: listId } });
  console.log(`✓ created ${t.id}\t${t.name}`);
  console.log(`  ${t.url}`);
}

async function cmdCreateList(folderId, flags) {
  if (typeof flags.name !== 'string') fail('create-list requires --name "<list name>"');
  const body = { name: flags.name };
  if (typeof flags.desc === 'string') body.content = flags.desc;
  const l = await cu(`/folder/${folderId}/list`, { method: 'POST', body: JSON.stringify(body) });
  console.log(`✓ created list ${l.id}\t${l.name}`);
}

async function cmdUpdate(taskId, flags) {
  const body = {};
  if (typeof flags['desc-file'] === 'string') body.markdown_description = readFileSync(flags['desc-file'], 'utf8');
  else if (typeof flags.desc === 'string') body.markdown_description = flags.desc;
  if (typeof flags.name === 'string') body.name = flags.name;
  if (typeof flags.status === 'string') body.status = flags.status;
  if (typeof flags.priority === 'string') {
    const pr = PRIORITY[flags.priority.toLowerCase()];
    if (!pr) fail(`unknown priority "${flags.priority}" (use urgent|high|normal|low)`);
    body.priority = pr;
  }
  // Assignees on PUT use the add/rem shape (a bare array is ignored by ClickUp).
  if (typeof flags.assignee === 'string') {
    const ids = flags.assignee.split(',').map((s) => Number(resolvePerson(s.trim())));
    body.assignees = { add: ids, rem: [] };
  }
  if (typeof flags.due === 'string') {
    const ms = Date.parse(flags.due);
    if (Number.isNaN(ms)) fail(`could not parse --due "${flags.due}" (try YYYY-MM-DD)`);
    body.due_date = ms;
  }
  if (!Object.keys(body).length) fail('update needs at least one of --desc, --desc-file, --name, --status, --priority, --assignee, --due');
  if (typeof body.status === 'string') await guardStatusOrFail(taskId, body.status);
  const t = await cu(`/task/${taskId}`, { method: 'PUT', body: JSON.stringify(body) });
  markWrite();
  const patch = {};
  if (typeof body.status === 'string') patch.status = body.status;
  if (typeof body.name === 'string') patch.name = body.name;
  if (Object.keys(patch).length) patchTask(taskId, patch);
  console.log(`✓ updated ${t.id}\t${t.name}`);
}

async function cmdComment(taskId, text) {
  if (!text) fail('comment requires the comment text as the second argument');
  guardCommentOrFail(text);
  await cu(`/task/${taskId}/comment`, { method: 'POST', body: JSON.stringify({ comment_text: text, notify_all: false }) });
  console.log(`✓ commented on ${taskId}`);
}

async function cmdComments(taskId, flags) {
  const { comments } = await cu(`/task/${taskId}/comment`);
  if (flags.json) return console.log(json(comments));
  if (!comments?.length) return console.log('(no comments)');
  for (const c of comments) console.log(`— ${c.user?.username ?? '?'}: ${c.comment_text}`);
}

async function cmdStatus(taskId, status) {
  if (!status) fail('status requires the status name as the second argument');
  await guardStatusOrFail(taskId, status);
  await cu(`/task/${taskId}`, { method: 'PUT', body: JSON.stringify({ status }) });
  markWrite();
  patchTask(taskId, { status });
  console.log(`✓ ${taskId} → ${status}`);
}

async function cmdTag(taskId, tagName) {
  if (!tagName) fail('tag requires <taskId> <tagName>');
  const enc = encodeURIComponent(tagName);
  let r = await cu(`/task/${taskId}/tag/${enc}`, { method: 'POST', soft: true });
  if (!r.ok) {
    // Tag probably doesn't exist at the space level yet — create it there once, then retry.
    const t = await cu(`/task/${taskId}`);
    const spaceId = t.space?.id;
    if (spaceId) {
      await cu(`/space/${spaceId}/tag`, {
        method: 'POST', soft: true,
        body: JSON.stringify({ tag: { name: tagName, tag_bg: '#7C4DFF', tag_fg: '#FFFFFF' } }),
      });
      r = await cu(`/task/${taskId}/tag/${enc}`, { method: 'POST', soft: true });
    }
    if (!r.ok) fail(`could not add tag "${tagName}" to ${taskId} (status ${r.status}): ${JSON.stringify(r.body)}`);
  }
  markWrite();
  console.log(`✓ ${taskId} +tag ${tagName}`);
}

async function cmdUntag(taskId, tagName) {
  if (!tagName) fail('untag requires <taskId> <tagName>');
  await cu(`/task/${taskId}/tag/${encodeURIComponent(tagName)}`, { method: 'DELETE' });
  markWrite();
  console.log(`✓ ${taskId} -tag ${tagName}`);
}

/**
 * Resolve a handoff role ('review' | 'wip') to the status name THIS task's list
 * actually uses. Lists vary ("Ready for Review" vs "in review", Title-case vs
 * lowercase, "complete" vs "done"), so a hardcoded label 400s on mismatch.
 * Returns { name, statuses } — name is null if no status matches the role.
 */
async function resolveHandoffStatus(taskId, role) {
  const t = await cu(`/task/${taskId}`);
  const listId = t.list?.id;
  if (!listId) fail(`could not determine the list for ${taskId} (no list.id on the task)`);
  const list = await listMeta(listId);
  const statuses = (list.statuses ?? []).map((s) => s.status).filter(Boolean);
  const lc = statuses.map((s) => s.toLowerCase());
  const pick = (...preds) => {
    for (const pred of preds) {
      const i = lc.findIndex(pred);
      if (i !== -1) return statuses[i];
    }
    return null;
  };
  let name = null;
  if (role === 'review') {
    name = pick(
      (s) => s === 'ready for review',
      (s) => s === 'in review',
      (s) => s === 'review',
      (s) => s.includes('review'),
    );
  } else if (role === 'wip') {
    name = pick(
      (s) => s === 'in progress',
      (s) => s.includes('progress'),
      (s) => s === 'wip',
    );
  }
  return { name, statuses };
}

async function cmdHandoff(taskId, role, comment) {
  const { name, statuses } = await resolveHandoffStatus(taskId, role);
  if (!name) {
    fail(`no "${role}" status on this task's list. Available: ${statuses.map((s) => `"${s}"`).join(', ')}. ` +
         `Use: node clickup.mjs status ${taskId} "<one of those>"`);
  }
  // Guardrails FIRST — validate both the status transition and the comment body
  // before any write, so a blocked guard never leaves a half-applied hand-off.
  await guardStatusOrFail(taskId, name);
  if (comment) guardCommentOrFail(comment);
  // Set status FIRST so a bad status never leaves an orphaned comment behind.
  await cu(`/task/${taskId}`, { method: 'PUT', body: JSON.stringify({ status: name }) });
  markWrite();
  patchTask(taskId, { status: name });
  if (comment) await cu(`/task/${taskId}/comment`, { method: 'POST', body: JSON.stringify({ comment_text: comment, notify_all: false }) });
  console.log(`✓ ${taskId} → ${name}${comment ? ' (+ comment)' : ''}`);
}

async function cmdDelete(taskId) {
  if (!taskId) fail('delete requires <taskId>');
  await cu(`/task/${taskId}`, { method: 'DELETE' });
  markWrite();
  removeTask(taskId);
  console.log(`✓ deleted ${taskId}`);
}

/** Print the co-located mapping. `ids`, `ids people`, `ids lists`, `ids <client>`. */
function cmdIds(which) {
  const m = ids();
  if (!which) {
    console.log(`workspace: ${m.workspace?.name} (${m.workspace?.id})`);
    console.log(`people:    ${Object.keys(m.people ?? {}).join(', ')}`);
    console.log(`spaces:    ${Object.keys(m.spaces ?? {}).join(', ')}`);
    console.log(`clients:   ${Object.keys(m.clients ?? {}).join(', ')}`);
    console.log(`aliases:   ${Object.keys(m.lists ?? {}).join(', ')}`);
    console.log(`\nUse:  node clickup.mjs ids people | ids lists | ids <client>`);
    return;
  }
  if (which === 'people') { for (const [k, v] of Object.entries(m.people ?? {})) console.log(`${k}\t${v.id}\t${v.name} <${v.email}>`); return; }
  if (which === 'lists') { for (const [k, v] of Object.entries(m.lists ?? {})) console.log(`${k}\t${v}`); return; }
  const c = m.clients?.[which];
  if (!c) fail(`unknown client "${which}". Known: ${Object.keys(m.clients ?? {}).join(', ')}`);
  console.log(`${which}  folder ${c.folder}`);
  for (const [k, v] of Object.entries(c.lists ?? {})) console.log(`  ${k}\t${v}`);
}

/** Cache management: `cache` (stats), `cache clear`, `cache path`. */
function cmdCache(which) {
  if (which === 'clear') { clearCache(); console.log(`✓ cleared cache at ${cacheDir()}`); return; }
  if (which === 'path') { console.log(cacheDir()); return; }
  const s = cacheStats();
  const ix = indexStats();
  console.log(`dir:        ${s.dir}`);
  console.log(`entries:    ${s.files}`);
  console.log(`size:       ${(s.bytes / 1024).toFixed(1)} KiB`);
  console.log(`disabled:   ${s.disabled} (CLICKUP_NO_CACHE)`);
  console.log(`last write: ${s.lastWriteTs ? new Date(s.lastWriteTs).toISOString() : '(none)'}`);
  console.log(`task index: ${ix.tasks} tasks across ${ix.lists} list(s) — ${ix.dir}`);
  console.log(`\nBoard pulls cache ${BOARD_TTL_MS / 1000}s (evicted by any local write); list metadata ${META_TTL_MS / 86400000}d.`);
  console.log(`Bypass a read: list … --fresh | --max-age <sec>. Disable all: CLICKUP_NO_CACHE=1.`);
}

const HELP = `clickup — light, portable ClickUp REST v2 CLI

Usage:  node clickup.mjs <command> [args]
Auth:   CLICKUP_API_TOKEN from the environment, else 1Password (op://Dev Toolbox/dev).

Discovery
  whoami                              verify the token (prints user)
  teams                               list workspaces (teams)
  spaces [teamId]                     list spaces in a team
  folders <spaceId|name>              list folders in a space
  lists <folderId>                    list lists in a folder
  ids [people|lists|<client>]         print the co-located name→id mapping

Tasks
  list <listId|client:key> [--status S] [--assignee me|name|id] [--include-closed] [--subtasks] [--json] [--compact] [--fresh] [--max-age S]
       (board pulls cache 60s and are evicted by any local write; --fresh bypasses, --max-age S overrides TTL)
       (--subtasks also returns child tasks, flattened in, each with a parent field)
       (--compact, with --json, strips custom_fields/description/text_content/markdown_description/
        attachments/checklists noise; keeps id/name/status/tags/parent/dates/etc for triage)
  find [query] [--list <id|client:key>] [--sync] [--json]
       local task lookup by id or name substring — NO API call. Populated by every
       create + list pull, so a task made in one session is found in another without
       pulling the board. --sync refreshes one board from the API first.
  task <taskId> [--subtasks] [--json] [--compact]
       (--subtasks attaches the full child-task list, with statuses, regardless of child status)
       (--compact, with --json, strips custom_fields/description/text noise on the task — and each
        subtask — but keeps id/name/status/tags/parent/dates for triage)
  create --list <id|client:key> --name "..." [--desc "md"]
         [--priority urgent|high|normal|low] [--status "..."]
         [--tags a,b,c] [--assignee name|id] [--due YYYY-MM-DD]
         (no --status → lands in the list's "to do", never deferred/backlog;
          --no-default-status reverts to the ClickUp list default)
  status <taskId> "<status name>"
  update <taskId> [--desc "md"|--desc-file path] [--name "..."] [--status "..."] [--priority p]
         (--desc-file reads markdown from a file — use for long/special-character
          descriptions that don't survive as an inline shell arg)
  tag <taskId> <tagName>              add a tag (auto-creates it at space level if new)
  untag <taskId> <tagName>            remove a tag

Comments & hand-off
  comment <taskId> "<text>"
  comments <taskId> [--json]
  review <taskId> ["comment"]         set the list's review status (auto-resolved) + optional comment
  wip <taskId> ["comment"]            set the list's in-progress status (auto-resolved) + optional comment

Cache
  cache [clear|path]                  show cache stats, clear it, or print its dir
       (list metadata caches 7d; board pulls 60s + write-eviction; CLICKUP_NO_CACHE=1 disables)

Names: --list fieldboss:seo, --assignee colin — resolved via references/ids.json.
Rate:  100 req/min/token; 429s auto-retry against X-RateLimit-Reset.`;

/** Upload a file as an attachment to a task (multipart; bypasses the JSON cu() wrapper). */
async function cmdAttach(taskId, filePath) {
  if (!filePath) fail('attach requires <taskId> <filePath>');
  const buf = readFileSync(filePath);
  const name = filePath.split(/[\\/]/).pop();
  const fd = new FormData();
  fd.append('attachment', new Blob([buf]), name);
  const res = await fetch(`${API}/task/${taskId}/attachment`, {
    method: 'POST',
    headers: { Authorization: token() },
    body: fd,
  });
  const text = await res.text();
  if (!res.ok) fail(`ClickUp POST /task/${taskId}/attachment → ${res.status}: ${text}`);
  const body = text ? JSON.parse(text) : {};
  console.log(`✓ attached ${name} → task ${taskId}`);
  if (body.url) console.log(`  ${body.url}`);
}

async function main() {
  const { _, flags } = parseArgs(process.argv.slice(2));
  const [cmd, a, b] = _;

  if (!cmd || cmd === 'help' || flags.help) { console.log(HELP); return; }

  switch (cmd) {
    case 'whoami': return cmdWhoami();
    case 'teams': return cmdTeams();
    case 'spaces': return cmdSpaces(a);
    case 'folders': return a ? cmdFolders(a) : fail('folders requires <spaceId|name>');
    case 'lists': return a ? cmdLists(a) : fail('lists requires <folderId>');
    case 'ids': return cmdIds(a);
    case 'list': return a ? cmdList(a, flags) : fail('list requires <listId|client:key>');
    case 'find': return cmdFind(a, flags);
    case 'task': return a ? cmdTask(a, flags) : fail('task requires <taskId>');
    case 'create': return cmdCreate(flags);
    case 'create-list': return a ? cmdCreateList(a, flags) : fail('create-list requires <folderId> --name "<name>"');
    case 'update': return a ? cmdUpdate(a, flags) : fail('update requires <taskId> [--desc|--name|--status|--priority]');
    case 'comment': return a ? cmdComment(a, b) : fail('comment requires <taskId> "<text>"');
    case 'comments': return a ? cmdComments(a, flags) : fail('comments requires <taskId>');
    case 'attach': return a ? cmdAttach(a, b) : fail('attach requires <taskId> <filePath>');
    case 'status': return a ? cmdStatus(a, b) : fail('status requires <taskId> "<status>"');
    case 'tag': return a ? cmdTag(a, b) : fail('tag requires <taskId> <tagName>');
    case 'untag': return a ? cmdUntag(a, b) : fail('untag requires <taskId> <tagName>');
    case 'delete': return a ? cmdDelete(a) : fail('delete requires <taskId>');
    case 'cache': return cmdCache(a);
    case 'review': return a ? cmdHandoff(a, 'review', b) : fail('review requires <taskId>');
    case 'wip': return a ? cmdHandoff(a, 'wip', b) : fail('wip requires <taskId>');
    default: fail(`unknown command "${cmd}". Run with no args for help.`);
  }
}

main().catch((e) => fail(e instanceof Error ? e.message : String(e)));
