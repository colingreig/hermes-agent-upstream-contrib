#!/usr/bin/env python3
"""
db_apply.py — safe, slug-scoped publish of a rewritten blog post to a DB-backed site.

WHY THIS EXISTS (2026-06-23): some sites (dynamics365group.com) publish blog content
by writing a row in a Neon `posts` table — the row write IS the publish, there is no
git deploy. The OpenCode→worktree→PR shape does not fit them. The first autonomous run
(task 86e1z6aec) had OpenCode emit a hand-written `UPDATE posts ... SET meta_description=...`
SQL file — but the real column is `description`, NOT `meta_description`. Executing
LLM-authored SQL against a live prod table is unsafe (wrong/hallucinated columns,
unscoped WHERE, multi-row writes, DDL). So this lane NEVER executes LLM SQL.

DESIGN — structured apply, not SQL:
  * Input is a JSON object of CONTENT FIELDS (title/description/content/...), not SQL.
  * Only a WHITELIST of content columns may be written. Any other key is a hard error
    (this is exactly what catches the `meta_description` hallucination).
  * One transaction: SELECT ... FOR UPDATE the single slug row (assert exactly 1),
    parameterized UPDATE of only the provided whitelisted columns + updated_at=now(),
    assert rowcount == 1, verify-after (re-SELECT), then COMMIT. Any deviation ROLLBACKs.
  * --dry-run does the whole transaction then ROLLBACK — proves the apply without writing.
  * Values are always passed as psycopg2 params (never string-formatted into SQL).

AUTH: reads the connection string from --db-url-env's env vars (defaults to
$DATABASE_URL_UNPOOLED preferred, then $DATABASE_URL). 1Password injects these into
the caller's ambient env at gateway boot (op-secrets.env) — no doppler wrapper needed
since Doppler's decommission (2026-07-03).

Exit codes: 0 = applied (or dry-run validated).  2 = slug not found / nothing to do (soft).
            3 = guard tripped / DB error (hard).   4 = usage / setup error.

Usage:
  python3 ~/.hermes/scripts/db_apply.py \
      --table posts --slug macbook-vs-windows-business-central \
      --fields-file /path/to/fields.json [--expect-id g6Q0...] [--dry-run]

  fields.json: {"title": "...", "description": "...", "content": "...", "excerpt": "..."}
"""
import argparse
import json
import os
import re
import sys

try:
    import psycopg2
    from psycopg2.extras import Json
except Exception as e:  # psycopg2 missing — almost always the WRONG interpreter under cron.
    # SELF-HEAL (2026-06-23): a bare `python3` under the cron resolves to the system
    # /usr/bin/python3, which lacks psycopg2; the hermes venv python3.11 HAS it (2.9.12).
    # Re-exec THIS script under the venv interpreter (ONCE — env-flag guarded against a
    # loop) so db_apply works no matter which python invokes it. This is the bulletproof
    # fix for "No module named psycopg2" — the executor kept calling a bare python3 despite
    # the skill pinning the venv path; the script now corrects the interpreter itself.
    _VENV_PY = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python3.11")
    if (not os.environ.get("DB_APPLY_REEXEC")
            and os.path.exists(_VENV_PY)
            and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY)):
        os.environ["DB_APPLY_REEXEC"] = "1"
        try:
            os.execv(_VENV_PY, [_VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
        except Exception:
            pass  # exec failed — fall through to the hard error below
    print(json.dumps({"ok": False, "error": f"psycopg2 import failed: {e}"}))
    sys.exit(4)

# Content columns this lane is permitted to write. Deliberately EXCLUDES status,
# published_at, id, slug, created_at — this lane rewrites content, it does not
# (un)publish, re-slug, or move rows. Verified against the live schema 2026-06-23.
# These are the DEFAULTS (dynamics365group.com `posts`); a caller may pass a
# site-specific whitelist via --allowed-cols (db_publish_task passes cfg.allowed_cols).
TEXT_COLS = {"title", "description", "content", "excerpt", "category"}
JSONB_COLS = {"tags", "faq_data"}
DEFAULT_ALLOWED_COLS = TEXT_COLS | JSONB_COLS

# The executor is an LLM; it emits reasonable-but-non-canonical field names
# (`meta_description` for the meta column, `content_markdown` for the body). Map
# those predictable synonyms to the real column rather than hard-fail — but keep
# the hard-fail for any key that's STILL unknown after mapping (catches a true
# hallucination / typo, which signals a bad generation we should not write).
COLUMN_ALIASES = {
    "meta_description": "description", "metadescription": "description",
    "meta": "description", "seo_description": "description",
    "content_markdown": "content", "content_md": "content", "body": "content",
    "markdown": "content", "body_markdown": "content", "post_content": "content",
    "faq": "faq_data", "faqs": "faq_data", "faq_json": "faq_data",
    "headline": "title",
}
# Known sidecar/metadata keys an LLM deliverable includes alongside the content
# that are NOT post columns. Silently ignored (not hallucinations — just extra
# context). `updated_at` is always set to now() by the UPDATE itself.
IGNORE_KEYS = {
    "slug", "id", "updatedat", "updated_at", "createdat", "created_at",
    "internal_links", "external_links", "links", "sources", "url", "path",
    "score_estimate", "score", "audit_score", "_note", "_comment",
    "note", "comment", "status", "published_at",
}

# A rewrite that would leave the post body near-empty is almost certainly a bad
# generation, not an intentional edit — refuse it rather than wipe a live post.
MIN_CONTENT_LEN = 200


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


def fail(result, code, msg):
    result["error"] = msg
    print(json.dumps(result))
    return code


def main():
    ap = argparse.ArgumentParser(description="Safe slug-scoped publish of a blog post to a DB-backed site.")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--fields-file", required=True, help="JSON object of content fields to write.")
    ap.add_argument("--table", default="posts", help="Content table (default 'posts'; site-specific via db_publish_task).")
    ap.add_argument("--allowed-cols", default=None,
                    help="Comma-separated content-column whitelist (default: the dynamics365group.com posts set). "
                         "Pass a site-specific list to support another DB-backed site.")
    ap.add_argument("--db-url-env", default=None,
                    help="Comma-separated, ordered env var names for the connection string (first set wins). "
                         "Default: DATABASE_URL_UNPOOLED,DATABASE_URL.")
    ap.add_argument("--expect-id", default=None, help="If set, the resolved row id must equal this (defense in depth).")
    ap.add_argument("--dry-run", action="store_true", help="Run the full transaction then ROLLBACK; write nothing.")
    ap.add_argument("--backup-dir", default=os.path.expanduser("~/.hermes/db-backups"),
                    help="On a real apply, dump the full pre-image row here first (restore path). Set '' to skip.")
    args = ap.parse_args()

    result = {"ok": False, "slug": args.slug, "table": args.table,
              "dry_run": args.dry_run, "columns_updated": []}

    # --- table identifier guard: a valid SQL identifier only (no arbitrary SQL).
    # The lane is no longer posts-ONLY (a site may use a different content table),
    # but the table name is interpolated into SQL (cannot be a bound param), so it
    # MUST be a bare identifier — reject anything else to keep injection impossible.
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", args.table or ""):
        return fail(result, 4, f"refusing unsafe table identifier {args.table!r}")
    table = args.table

    # --- resolve the per-site content-column whitelist (default = dynamics posts) ---
    if args.allowed_cols:
        allowed_cols = {c.strip() for c in args.allowed_cols.split(",") if c.strip()}
        # JSONB vs text classification: anything in the default JSONB set stays JSONB;
        # everything else is treated as text. (Covers tags/faq_data; extend here if a
        # new site adds JSONB columns beyond those names.)
        jsonb_cols = {c for c in allowed_cols if c in JSONB_COLS}
        text_cols = allowed_cols - jsonb_cols
    else:
        allowed_cols = set(DEFAULT_ALLOWED_COLS)
        jsonb_cols = set(JSONB_COLS)
        text_cols = set(TEXT_COLS)

    # --- load + validate fields ---
    try:
        with open(os.path.expanduser(args.fields_file)) as f:
            fields = json.load(f)
    except Exception as e:
        return fail(result, 4, f"could not read fields-file: {e}")
    if not isinstance(fields, dict) or not fields:
        return fail(result, 4, "fields-file must be a non-empty JSON object")

    # Normalize keys: ignore known sidecar metadata, map predictable synonyms to
    # the real column, then hard-fail on anything STILL unknown.
    normalized, applied_aliases = {}, {}
    for raw_k, v in fields.items():
        k = str(raw_k).strip().lower()
        if k in IGNORE_KEYS:
            continue
        mapped = COLUMN_ALIASES.get(k, k)
        if mapped != k:
            applied_aliases[raw_k] = mapped
        if mapped in normalized:
            return fail(result, 3, f"duplicate column after alias mapping: {mapped!r}")
        normalized[mapped] = v
    fields = normalized
    if applied_aliases:
        result["aliased"] = applied_aliases

    # Unknown keys survive alias+ignore. Historically this HARD-FAILED the whole
    # write (exit 3) — but in practice the executor's LLM routinely emits extra
    # sidecar keys it hasn't been taught to strip (e.g. `solution_link`,
    # `change_summary`), and one stray key would abort an otherwise-valid publish.
    # That single brittleness was the conversion bug for the entire [Blog rewrite]
    # backlog (verified 2026-06-24 task 86e1z6ad9: `solution_link` aborted a good
    # rewrite → columns_updated:[] → task never shipped). FIX (Agent A 2026-06-24):
    # DROP unknown sidecar keys (record them in `dropped_unknown` for audit) rather
    # than abort — but ONLY write the recognized whitelisted content columns. The
    # hallucination guard is preserved by the rule that NO unknown key is ever
    # written to the DB; we just no longer let its mere presence wipe the publish.
    unknown = [k for k in fields if k not in allowed_cols]
    if unknown:
        result["dropped_unknown"] = sorted(unknown)
        eprint(f"[db_apply] dropping unknown sidecar key(s) {sorted(unknown)} "
               f"(not in whitelist {sorted(allowed_cols)}); writing only recognized columns")
        for k in unknown:
            del fields[k]
    if not fields:
        return fail(result, 2, "no writable content fields after filtering — nothing to do")

    # Per-field sanity: no empty text fields; content must be substantial.
    for col in list(fields):
        if col in text_cols:
            v = fields[col]
            if not isinstance(v, str) or not v.strip():
                return fail(result, 3, f"column {col!r} is empty/blank — refusing to write")
            if col == "content" and len(v) < MIN_CONTENT_LEN:
                return fail(result, 3,
                            f"content is only {len(v)} chars (<{MIN_CONTENT_LEN}) — likely a bad generation, refusing")

    # Resolve the connection string from the site's ordered env-var list (first set
    # wins). Default preserves today's behavior: UNPOOLED preferred, DATABASE_URL fallback.
    env_order = ([e.strip() for e in args.db_url_env.split(",") if e.strip()]
                 if args.db_url_env else ["DATABASE_URL_UNPOOLED", "DATABASE_URL"])
    dsn = next((os.environ[e] for e in env_order if os.environ.get(e)), None)
    if not dsn:
        return fail(result, 4, f"no connection string in env {env_order} — check 1Password op-secrets.env")

    conn = None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=20)
        conn.autocommit = False
        cur = conn.cursor()

        # 1. Lock the single target row; assert exactly one.
        cur.execute(f"SELECT id, length(content), updated_at FROM {table} WHERE slug=%s FOR UPDATE",
                    (args.slug,))
        rows = cur.fetchall()
        if len(rows) == 0:
            conn.rollback()
            return fail(result, 2, f"no posts row with slug={args.slug!r}")
        if len(rows) > 1:
            conn.rollback()
            return fail(result, 3, f"{len(rows)} rows match slug={args.slug!r} — refusing ambiguous write")
        row_id, len_before, updated_before = rows[0]
        result["id"] = row_id
        result["content_len_before"] = len_before
        if args.expect_id and row_id != args.expect_id:
            conn.rollback()
            return fail(result, 3, f"row id {row_id!r} != --expect-id {args.expect_id!r}")

        # Pre-image backup (real applies only) — a full-row JSON restore path
        # so every autonomous write is reversible. Done inside the txn so the
        # snapshot is exactly consistent with what we're about to overwrite.
        if not args.dry_run and args.backup_dir:
            try:
                import time as _t
                bcur = conn.cursor()
                bcur.execute(f"SELECT row_to_json(p) FROM {table} p WHERE id=%s", (row_id,))
                pre = bcur.fetchone()[0]
                bcur.close()
                os.makedirs(os.path.expanduser(args.backup_dir), exist_ok=True)
                bpath = os.path.join(os.path.expanduser(args.backup_dir),
                                     f"{args.slug}-{row_id}-{_t.strftime('%Y%m%d-%H%M%S')}.json")
                with open(bpath, "w") as bf:
                    json.dump(pre, bf, default=str, indent=2)
                result["backup_file"] = bpath
            except Exception as e:
                conn.rollback()
                return fail(result, 3, f"pre-image backup failed (refusing to write without a restore path): {e}")

        # 2. Build the parameterized UPDATE of ONLY provided whitelisted columns.
        set_parts, params = [], []
        for col, val in fields.items():
            set_parts.append(f"{col} = %s")
            params.append(Json(val) if col in jsonb_cols else val)
        set_parts.append("updated_at = now()")
        sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE slug=%s AND id=%s"
        params.extend([args.slug, row_id])
        cur.execute(sql, params)
        if cur.rowcount != 1:
            conn.rollback()
            return fail(result, 3, f"UPDATE touched {cur.rowcount} rows (expected 1) — rolled back")
        result["columns_updated"] = sorted(fields.keys())

        # 3. Verify-after (still inside the txn, before commit).
        cur.execute(f"SELECT length(content), updated_at, title FROM {table} WHERE id=%s", (row_id,))
        len_after, updated_after, title_after = cur.fetchone()
        result["content_len_after"] = len_after
        result["title_after"] = title_after
        if "content" in fields and len_after != len(fields["content"]):
            conn.rollback()
            return fail(result, 3,
                        f"verify failed: content len {len_after} != written {len(fields['content'])} — rolled back")
        if updated_after <= updated_before:
            conn.rollback()
            return fail(result, 3, "verify failed: updated_at did not advance — rolled back")

        if args.dry_run:
            conn.rollback()
            result["ok"] = True
            result["note"] = "dry-run OK — all guards passed, transaction rolled back (nothing written)"
            print(json.dumps(result))
            return 0

        conn.commit()
        result["ok"] = True
        print(json.dumps(result))
        return 0

    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return fail(result, 3, f"DB error (rolled back): {type(e).__name__}: {e}")
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
