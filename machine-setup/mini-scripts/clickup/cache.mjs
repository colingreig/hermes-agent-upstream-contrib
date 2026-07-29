/**
 * cache.mjs — a tiny, atomic, per-key file cache for clickup.mjs.
 *
 * Why this exists: the CLI is invoked as a fresh process every time, so an
 * in-memory cache buys nothing across calls. This is a disk cache shared across
 * every concurrent CLI process on the machine (multiple agent sessions, Hermes).
 *
 * Two independent invalidation mechanisms, chosen per-entry by the caller:
 *
 *   1. TTL — every entry stores its fetch time; reads past `maxAgeMs` miss.
 *   2. Write-epoch — a single machine-local `last-write.ts` file is bumped by
 *      every mutating command (markWrite). A board read is served ONLY if it was
 *      cached AFTER the last write. So any write, from any process on this box,
 *      invalidates every board snapshot instantly → you never act on task data
 *      older than the most recent local write. Static metadata (list status
 *      definitions) opts OUT of this (ignoreEpoch) — those don't change when a
 *      task moves, so a task write should not evict them.
 *
 * Cross-MACHINE staleness (e.g. Hermes on the Mac mini writes a status) is NOT
 * caught by the epoch file (different filesystem) and is bounded only by the TTL.
 * That is inherent: ClickUp is the sole cross-machine source of truth, so a
 * per-machine cache can only be exactly-correct for same-machine writes. Keep
 * board TTLs short for that reason.
 *
 * Storage: one JSON file per key under a home-rooted, disposable cache dir
 * (never inside the plugin tree, which is wiped on every plugin update). Writes
 * are atomic (temp file + rename) so a concurrent reader never sees a torn file.
 */

import {
  readFileSync, writeFileSync, mkdirSync, renameSync, rmSync, readdirSync, statSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

const DIR = process.env.CLICKUP_CACHE_DIR
  || join(process.env.XDG_CACHE_HOME || join(homedir(), '.cache'), 'ignite-clickup');
const EPOCH_FILE = join(DIR, 'last-write.ts');

const DISABLED = /^(1|true|yes|on)$/i.test(process.env.CLICKUP_NO_CACHE ?? '');

function ensureDir() { try { mkdirSync(DIR, { recursive: true }); } catch { /* best-effort */ } }

/** Filesystem-safe path for a cache key (keys are already short + tame). */
function keyPath(key) { return join(DIR, `${key.replace(/[^\w.-]/g, '_')}.json`); }

/** Stable short hash for composing a cache key from a query string. */
export function hashKey(s) { return createHash('sha1').update(String(s)).digest('hex').slice(0, 12); }

function lastWriteTs() {
  try { return Number(readFileSync(EPOCH_FILE, 'utf8')) || 0; } catch { return 0; }
}

/**
 * Read a cached value, or null on any miss (absent, expired, invalidated, or
 * cache disabled). `maxAgeMs` null = no TTL. `ignoreEpoch` = don't apply the
 * write-epoch invalidation (use for static metadata).
 */
export function getCached(key, { maxAgeMs = null, ignoreEpoch = false } = {}) {
  if (DISABLED) return null;
  let raw;
  try { raw = JSON.parse(readFileSync(keyPath(key), 'utf8')); } catch { return null; }
  if (!raw || typeof raw.fetchedAt !== 'number') return null;
  const age = Date.now() - raw.fetchedAt;
  if (maxAgeMs != null && age > maxAgeMs) return null;
  if (!ignoreEpoch && raw.fetchedAt < lastWriteTs()) return null;
  return { data: raw.data, ageMs: age };
}

/** Write a value to the cache (atomic). No-op when caching is disabled. */
export function setCached(key, data) {
  if (DISABLED) return;
  ensureDir();
  const dst = keyPath(key);
  const tmp = `${dst}.${process.pid}.tmp`;
  try {
    writeFileSync(tmp, JSON.stringify({ v: 1, fetchedAt: Date.now(), data }));
    renameSync(tmp, dst);
  } catch { try { rmSync(tmp, { force: true }); } catch { /* ignore */ } }
}

/** Bump the machine-local write epoch. Call AFTER a successful mutating write. */
export function markWrite() {
  if (DISABLED) return;
  ensureDir();
  const tmp = `${EPOCH_FILE}.${process.pid}.tmp`;
  try {
    writeFileSync(tmp, String(Date.now()));
    renameSync(tmp, EPOCH_FILE);
  } catch { try { rmSync(tmp, { force: true }); } catch { /* ignore */ } }
}

export function clearCache() {
  try { rmSync(DIR, { recursive: true, force: true }); } catch { /* ignore */ }
}

export function cacheDir() { return DIR; }

/** Cheap stats for the `cache stats` command. */
export function cacheStats() {
  let files = 0; let bytes = 0;
  try {
    for (const f of readdirSync(DIR)) {
      if (!f.endsWith('.json')) continue;
      files++;
      try { bytes += statSync(join(DIR, f)).size; } catch { /* ignore */ }
    }
  } catch { /* dir absent */ }
  return { dir: DIR, files, bytes, disabled: DISABLED, lastWriteTs: lastWriteTs() };
}
