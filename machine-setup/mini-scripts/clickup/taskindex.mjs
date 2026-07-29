/**
 * taskindex.mjs — a persistent, local, write-through index of ClickUp tasks.
 *
 * The problem it solves: you create a task in one session and act on it in
 * another. Without this, the second session has to pull the WHOLE board over
 * the network and loop through it just to find that one task's id. That pull
 * happens on every run because a time-based cache can't span sessions that are
 * minutes apart. This index is not time-based — it is a durable record that is
 * updated the instant anything changes it, so a lookup is a local file read.
 *
 * How it stays correct without a server:
 *   - `create` upserts the new task (we get its real id back from the API).
 *   - `status` / `update` / `review` / `wip` patch that task's entry in place.
 *   - `delete` removes it.
 *   - a full board pull (`list` with no status/assignee filter) REPLACES the
 *     list's index — the authoritative resync point that also catches tasks
 *     created externally (by a human or by Hermes on another machine).
 *   - a filtered pull upserts what it returned but never drops the rest.
 *
 * So for the common case — you and your other sessions on THIS machine creating
 * and working tasks — the index is exact with no polling. The only thing it
 * can lag is a task created elsewhere since your last full `list`; run `list`
 * (or `find --sync`) to reconcile. ClickUp remains the source of truth; this is
 * a local read-through record, not an authority.
 *
 * Storage: one JSON file per list under <cacheDir>/index/, atomic writes.
 */

import {
  readFileSync, writeFileSync, mkdirSync, renameSync, rmSync, readdirSync,
} from 'node:fs';
import { join } from 'node:path';
import { cacheDir } from './cache.mjs';

const DIR = join(cacheDir(), 'index');

function ensureDir() { try { mkdirSync(DIR, { recursive: true }); } catch { /* best-effort */ } }
function idxPath(listId) { return join(DIR, `${String(listId).replace(/[^\w.-]/g, '_')}.json`); }

/** Normalize a raw ClickUp task object into a compact index entry. */
function toEntry(t) {
  return {
    id: t.id,
    name: t.name,
    status: t.status?.status ?? null,
    tags: Array.isArray(t.tags) ? t.tags.map((x) => x?.name).filter(Boolean) : [],
    parent: t.parent ?? null,
    url: t.url ?? null,
    listId: t.list?.id ?? null,
    listName: t.list?.name ?? null,
    updatedAt: Date.now(),
  };
}

function load(listId) {
  try { return JSON.parse(readFileSync(idxPath(listId), 'utf8')); }
  catch { return { listId: String(listId), tasks: {}, syncedAt: 0 }; }
}

function save(listId, obj) {
  ensureDir();
  const dst = idxPath(listId);
  const tmp = `${dst}.${process.pid}.tmp`;
  try { writeFileSync(tmp, JSON.stringify(obj)); renameSync(tmp, dst); }
  catch { try { rmSync(tmp, { force: true }); } catch { /* ignore */ } }
}

/** Upsert one task (from a create/update response) into its list's index. */
export function upsertTask(listId, rawTask) {
  if (!listId || !rawTask?.id) return;
  const idx = load(listId);
  idx.tasks[rawTask.id] = toEntry(rawTask);
  save(listId, idx);
}

/**
 * Patch fields of a task already in the index, located by scanning every index
 * file (callers of `status`/`update` have the taskId but not always the listId).
 * No-op if the task isn't indexed yet. Returns the listId it patched, or null.
 */
export function patchTask(taskId, fields) {
  if (!taskId) return null;
  let files = [];
  try { files = readdirSync(DIR).filter((f) => f.endsWith('.json')); } catch { return null; }
  for (const f of files) {
    const listId = f.replace(/\.json$/, '');
    const idx = load(listId);
    if (idx.tasks[taskId]) {
      idx.tasks[taskId] = { ...idx.tasks[taskId], ...fields, updatedAt: Date.now() };
      save(listId, idx);
      return listId;
    }
  }
  return null;
}

/** Remove a task from whichever index holds it. */
export function removeTask(taskId) {
  if (!taskId) return;
  let files = [];
  try { files = readdirSync(DIR).filter((f) => f.endsWith('.json')); } catch { return; }
  for (const f of files) {
    const listId = f.replace(/\.json$/, '');
    const idx = load(listId);
    if (idx.tasks[taskId]) { delete idx.tasks[taskId]; save(listId, idx); return; }
  }
}

/**
 * Reconcile a list's index from a board pull. `replace` (unfiltered pull) rebuilds
 * the list wholesale so externally-deleted/added tasks are caught; otherwise the
 * returned tasks are upserted without dropping the rest (a filtered pull is partial).
 */
export function syncFromBoard(listId, rawTasks, { replace }) {
  if (!listId || !Array.isArray(rawTasks)) return;
  const idx = replace ? { listId: String(listId), tasks: {}, syncedAt: Date.now() }
                      : load(listId);
  for (const t of rawTasks) if (t?.id) idx.tasks[t.id] = toEntry(t);
  idx.syncedAt = Date.now();
  save(listId, idx);
}

/**
 * Find tasks by exact id or case-insensitive name substring. Scans one list's
 * index when listId is given, else every index. Pure local read — no network.
 */
export function findTasks(query, { listId = null } = {}) {
  const q = String(query ?? '').toLowerCase().trim();
  const files = listId
    ? [`${String(listId).replace(/[^\w.-]/g, '_')}.json`]
    : (() => { try { return readdirSync(DIR).filter((f) => f.endsWith('.json')); } catch { return []; } })();
  const out = [];
  for (const f of files) {
    const idx = load(f.replace(/\.json$/, ''));
    for (const e of Object.values(idx.tasks)) {
      if (!q || e.id === query || (e.name ?? '').toLowerCase().includes(q)) out.push(e);
    }
  }
  return out;
}

export function indexDir() { return DIR; }

export function indexStats() {
  let lists = 0; let tasks = 0;
  try {
    for (const f of readdirSync(DIR)) {
      if (!f.endsWith('.json')) continue;
      lists++;
      tasks += Object.keys(load(f.replace(/\.json$/, '')).tasks ?? {}).length;
    }
  } catch { /* dir absent */ }
  return { dir: DIR, lists, tasks };
}
