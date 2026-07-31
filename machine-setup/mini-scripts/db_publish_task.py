#!/usr/bin/env python3
"""
db_publish_task.py — ONE deterministic call that runs the WHOLE DB-backed blog publish.

WHY THIS EXISTS (Agent A3, 2026-06-24):
The DB-publish lane (dynamics365group.com blog rewrites: a Neon `posts` row write IS the
publish, no git deploy) was a 6-substep recipe the gpt-5-mini orchestrator had to improvise
each tick — and it fumbled the choreography EVERY tick: malformed `&` commands, reading a
prompt file it never wrote, skipping the `mkdir -p` (→ opencode_exec `exit 4 workdir not
found`), execute_code json_parse_failed. Lanes were correct (SKILL step 3c routed it), the
helpers were correct (opencode_exec + db_apply both verified), but the GLUE between them was
LLM-authored and non-deterministic → ~0 tasks shipped.

This script collapses the mechanical core into ONE atomic, deterministic call so the
orchestrator only does: claim (before) → THIS SCRIPT → closeout comment + `clickup.mjs
review` (after). No LLM choreography in the middle.

WHAT IT DOES (atomic, no LLM glue):
  1. mkdir -p ~/.hermes/worktrees/ignite-<taskId>                       (kills the exit-4 round trip)
  2. Fetch the task (clickup.mjs task --json); derive --slug from /blog/<slug> if omitted.
  3. Pull the current post + 1-2 sibling posts (READ-ONLY) from `posts` as voice/exemplars,
     and assemble the CONTENT prompt (brief verbatim + exemplars + expansive writer
     instruction + the EXACT allowed-columns + fields.json output contract). Writes the
     prompt to a script-controlled temp path (NOT inside the workdir).
  4. opencode_exec.py --workdir ~/.hermes/worktrees/ignite-<taskId> --prompt-file <p> --task-id <id> --content
     (Sonnet-only, fail-closed content route). Require ok:true AND mode=="db-publish" AND a non-empty
     fields_file. Any failure → clear diagnostic JSON, non-zero exit, do NOT proceed.
  5. db_apply.py (absolute venv python; DB URL from ambient env, 1Password-injected at
     gateway boot) on the fields_file for --slug. Honors --dry-run. Require ok:true.
  6. Print ONE structured result JSON and exit 0 on ok:true.

WHAT IT DOES NOT DO (left to the SKILL, on purpose — identity/comment/review-gate discipline):
  * Does NOT claim the task or post the `ignite-` claim comment (executor does this BEFORE).
  * Does NOT flip ClickUp status / post the closeout (executor does this AFTER on ok:true).

Exit codes: 0 = ok (applied, or dry-run validated).  Non-zero = a clear diagnostic JSON on
stdout with `stage` telling you exactly where it stopped.

Usage:
  python3 ~/.hermes/scripts/db_publish_task.py --task-id 86e1z6ada \
      --slug microsoft-dynamics-office-add-in-error [--dry-run]
  (slug optional if the task body contains a /blog/<slug> URL; --prompt-file optional to
   bypass auto-assembly and supply your own.)
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_site_config as sites

HOME = os.path.expanduser("~")
SCRIPTS = os.path.join(HOME, ".hermes", "scripts")
DELIVERABLES = os.path.join(HOME, ".hermes", "deliverables")
VENV_PY = os.path.join(HOME, ".hermes", "hermes-agent", "venv", "bin", "python3.11")
CLICKUP = os.path.join(HOME, ".claude", "skills", "clickup", "clickup.mjs")
OPENCODE_EXEC = os.path.join(SCRIPTS, "opencode_exec.py")
DB_APPLY = os.path.join(SCRIPTS, "db_apply.py")
# NOTE: per-site values (live_url_base, table, allowed_cols, slug pattern, db env,
# publication name) now come from db_site_config.SITE_CONFIG via the routing guard —
# no longer hardcoded to dynamics365group.com. See db_site_config.py.


def out(result, code):
    """Print the single structured result JSON and return the exit code."""
    print(json.dumps(result))
    sys.stdout.flush()
    return code


def _progress(stage, task_id, note=""):
    """Emit a timestamped progress marker to stderr — diagnosable by the executor terminal
    and babysit even if the script hangs before printing the final JSON (PATCH 2026-06-30:
    each major stage now logs here so 'no output' hangs are pinpointed, not just 'stuck in
    db_publish_task.py somewhere'). Goes to stderr so it never corrupts the stdout result JSON."""
    import time as _time
    ts = _time.strftime("%H:%M:%S")
    msg = f"[db_publish_task] {ts} stage={stage} task={task_id}"
    if note:
        msg += f" {note}"
    print(msg, file=sys.stderr, flush=True)


def run(cmd, timeout=120, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def fetch_task(task_id):
    """Return the raw ClickUp task dict via clickup.mjs, or None."""
    try:
        r = run(["node", CLICKUP, "task", task_id, "--json"], timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def db_read(sql, params, db_url_env=None):
    """Read-only query via the venv python; returns rows (list) or [].

    db_url_env: ordered list of env var names for the connection string (the SITE's
    db_url_env, e.g. from db_site_config). Default preserves the historic generic
    names for callers that haven't resolved a site yet."""
    env_order = db_url_env or ["DATABASE_URL_UNPOOLED", "DATABASE_URL"]
    pyprog = (
        "import os,json,psycopg2\n"
        f"dsn=next((os.environ[e] for e in {env_order!r} if os.environ.get(e)),None)\n"
        "c=psycopg2.connect(dsn,connect_timeout=20);cur=c.cursor()\n"
        f"cur.execute({sql!r}, {params!r})\n"
        "print(json.dumps([list(r) for r in cur.fetchall()],default=str))\n"
        "c.close()\n"
    )
    try:
        # 1Password already injects the site's DB URL into this process's ambient env
        # (gateway boots under `op run --env-file=...`); no doppler wrapper needed
        # since Doppler's decommission (2026-07-03).
        r = run([VENV_PY, "-c", pyprog], timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return []


def assemble_prompt(task, slug, current_post, exemplars, cfg):
    """Build a self-contained CONTENT prompt that yields a fields.json with allowed columns."""
    name = (task or {}).get("name", slug)
    body = (task or {}).get("description") or (task or {}).get("text_content") or ""
    parts = []
    parts.append(f"You are an expert writer for {cfg.publication_name}.")
    parts.append(f"Task: {name}\nTask ID: {(task or {}).get('id','')}\nTarget post slug: {slug}\n")
    parts.append("=== BRIEF + ACCEPTANCE CRITERIA (deliver all of this) ===\n" + (body.strip() or "(no body provided — rewrite the existing post below to be more specific, verdict-first, and on-voice.)"))
    if current_post:
        cur_title, cur_desc, cur_content = current_post
        parts.append("=== CURRENT POST (this is the piece you are rewriting/improving) ===\n"
                     f"title: {cur_title}\ndescription: {cur_desc}\n\n{(cur_content or '')[:6000]}")
    if exemplars:
        ex_blocks = []
        for (et, ec) in exemplars[:2]:
            ex_blocks.append(f"--- exemplar: {et} ---\n{(ec or '')[:4000]}")
        parts.append("=== PUBLISHED EXEMPLARS FROM THIS COLLECTION (match this depth + voice; do NOT copy text) ===\n"
                     + "\n\n".join(ex_blocks))
    parts.append(
        "You are an expert writer for this publication. Write the BEST possible version of this piece — "
        "fully developed, specific, on-voice, matching the depth of the exemplars. Be expansive where the "
        "subject earns it. Do NOT artificially truncate the prose. Ground every claim; invent no specs/quotes/stats. "
        "Avoid AI-tell filler (no 'in today's fast-paced world', 'it's worth noting', 'dive into', 'unlock', 'delve', hollow tricolons)."
    )
    parts.append(
        "=== OUTPUT CONTRACT (CRITICAL — follow EXACTLY) ===\n"
        "Use apply_patch to CREATE the file `fields.json` in the current working directory (the --dir workdir). "
        "It MUST be a single JSON object whose keys are ONLY from this set:\n"
        f"  {', '.join(cfg.allowed_cols)}\n"
        "Where: title = post title; description = the meta description (~150-160 chars); content = the full rewritten "
        "markdown body (>= 200 chars); excerpt/category optional; tags/faq_data optional JSON. "
        "Do NOT include any other keys (no slug, no meta_description, no internal_links, no solution_link, no status). "
        "Do NOT write SQL. Do NOT create any other deliverable file. Output ONLY fields.json."
    )
    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="One deterministic DB-backed blog publish (mkdir→prompt→opencode→db_apply).")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--slug", default=None, help="Target slug. If omitted, derived from the task's post URL via the site's slug pattern.")
    ap.add_argument("--table", default=None, help="Override the site's content table (default: from SITE_CONFIG).")
    ap.add_argument("--dry-run", action="store_true", help="db_apply runs the full txn then ROLLBACK (writes nothing).")
    ap.add_argument("--prompt-file", default=None, help="Optional: supply a prompt file instead of auto-assembly.")
    ap.add_argument("--timeout", type=int, default=int(os.environ.get("OPENCODE_TIMEOUT", "900")))
    args = ap.parse_args()

    result = {"ok": False, "task_id": args.task_id, "slug": args.slug,
              "dry_run": args.dry_run, "stage": "init"}

    _progress("start", args.task_id)

    # 1. Workdir (deterministic; removes the exit-4 round trip) ----------------
    # 2026-07-03: moved off ~/dev (which is the canonical one-folder-per-repo home for
    # real repos) into ~/.hermes/worktrees, the dedicated home for ephemeral per-task
    # workdirs. See ~/.hermes/skills/clickup-queue-poller/references/worktree-setup-pitfall.md.
    workdir = os.path.join(HOME, ".hermes", "worktrees", f"ignite-{args.task_id}")
    try:
        os.makedirs(workdir, exist_ok=True)
    except Exception as e:
        result["stage"] = "mkdir"; result["error"] = f"could not create workdir {workdir}: {e}"
        return out(result, 4)
    result["workdir"] = workdir
    _progress("workdir_ok", args.task_id)

    # 2. Fetch task, then ROUTE BY SITE (fail-safe-LOUD anti-strand guard) ------
    # Derive the target site from the task body's post URL. If the site is NOT in
    # SITE_CONFIG (or no URL is derivable), PARK LOUDLY: do NOT mis-route against the
    # wrong DB/domain and do NOT enter any live-URL/db retry. Leave the task 'to do'.
    _progress("fetch_task_start", args.task_id)
    task = fetch_task(args.task_id)
    _progress("fetch_task_done", args.task_id, f"task={'ok' if task else 'None'}")
    cfg, park = sites.guard_resolve_site(task)
    if park is not None:
        result.update(park)  # ok:false, stage:'site_route', park:true, reason, comment, ...
        result["error"] = park["reason"]
        return out(result, 2)  # exit 2 = soft/park (claimable), NEVER a retry-forever hard fail
    result["site"] = cfg.domain
    table = args.table or cfg.table
    result["table"] = table

    # 2b. Slug — explicit override, else derived via the SITE's slug pattern.
    slug = args.slug or sites.derive_slug(task, cfg)
    if not slug:
        result["stage"] = "slug"
        result["error"] = (f"no --slug given and none derivable from the task body via "
                           f"{cfg.domain}'s slug pattern ({cfg.slug_url_pattern.pattern}) "
                           f"— park as 'needs target slug'")
        return out(result, 2)
    result["slug"] = slug
    result["live_url"] = cfg.live_url(slug)

    # 2c. Read-only: current post (the piece being rewritten) + sibling exemplars.
    # Table name is from SITE_CONFIG, sanitized (identifier — cannot be parameterized).
    _progress("db_read_current_post_start", args.task_id, f"slug={slug}")
    safe_table = "".join(ch for ch in table if ch.isalnum() or ch == "_")
    current_post, exemplars = None, []
    rows = db_read(f"SELECT title, description, content FROM {safe_table} WHERE slug=%s", [slug],
                   db_url_env=cfg.db_url_env)
    if rows:
        current_post = rows[0]
    else:
        # Confirm-existence guard: db_apply will exit 2 anyway, but fail fast + clearly.
        result["stage"] = "slug_lookup"
        result["error"] = f"no {safe_table} row with slug={slug!r} — slug-derivation miss, park (do NOT broaden)"
        return out(result, 2)
    _progress("db_read_exemplars_start", args.task_id)
    ex_rows = db_read(
        f"SELECT title, content FROM {safe_table} WHERE slug<>%s AND length(content)>1500 "
        "ORDER BY length(content) DESC LIMIT 2", [slug], db_url_env=cfg.db_url_env)
    exemplars = [(r[0], r[1]) for r in ex_rows]
    _progress("db_read_done", args.task_id, f"current={'ok' if current_post else 'missing'} exemplars={len(exemplars)}")

    # 3. Prompt ----------------------------------------------------------------
    if args.prompt_file:
        prompt_file = os.path.abspath(os.path.expanduser(args.prompt_file))
        if not os.path.isfile(prompt_file):
            result["stage"] = "prompt"; result["error"] = f"--prompt-file not found: {prompt_file}"
            return out(result, 4)
    else:
        prompt = assemble_prompt(task, slug, current_post, exemplars, cfg)
        prompt_file = os.path.join("/tmp", f"oc_prompt_{args.task_id}.txt")
        try:
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(prompt)
        except Exception as e:
            result["stage"] = "prompt"; result["error"] = f"could not write prompt file: {e}"
            return out(result, 4)
    result["prompt_file"] = prompt_file

    # 4. OpenCode (Sonnet-only, fail-closed content route via --content) ---------
    # --content selects the Sonnet-only, fail-closed route for file-based content.
    result["stage"] = "opencode"
    _progress("opencode_start", args.task_id, f"timeout={args.timeout}s")
    try:
        oc = run([VENV_PY, OPENCODE_EXEC, "--workdir", workdir,
                  "--prompt-file", prompt_file, "--task-id", args.task_id, "--content",
                  "--timeout", str(args.timeout)],
                 timeout=args.timeout + 120)
    except Exception as e:
        result["error"] = f"opencode_exec invocation failed: {e}"
        return out(result, 3)
    oc_json = None
    for line in reversed((oc.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                oc_json = json.loads(line); break
            except Exception:
                continue
    if oc_json is None:
        result["error"] = f"opencode_exec produced no parseable JSON; stderr_tail={(oc.stderr or '')[-600:]}"
        return out(result, 3)
    result["opencode"] = {k: oc_json.get(k) for k in
                          ("ok", "mode", "db_publish", "fields_file", "deliverables",
                           "workdir_is_git", "stop_reason", "error", "log", "writer_cascade")}
    if not oc_json.get("ok"):
        result["error"] = f"opencode_exec ok:false — {oc_json.get('error')}"
        return out(result, 3)
    if oc_json.get("mode") != "db-publish" or not oc_json.get("db_publish"):
        result["error"] = (f"opencode_exec did not produce a DB-publish deliverable "
                           f"(mode={oc_json.get('mode')}, db_publish={oc_json.get('db_publish')}, "
                           f"deliverables={oc_json.get('deliverables')}) — expected fields.json in a non-git workdir")
        return out(result, 3)
    fields_file = oc_json.get("fields_file")
    if not fields_file or not os.path.isfile(fields_file) or os.path.getsize(fields_file) == 0:
        result["error"] = f"fields_file missing/empty: {fields_file!r}"
        return out(result, 3)
    result["fields_file"] = fields_file
    _progress("opencode_done", args.task_id, f"ok={oc_json.get('ok')} fields={fields_file}")

    # 5. db_apply (venv python; honors --dry-run) -------------------------------
    # Pass the SITE's table + allowed-cols + db-url env order so db_apply writes the
    # right table in the right DB with the right whitelist (no longer posts-only).
    # 1Password already injects the site's DB URL into this process's ambient env
    # (gateway boots under `op run --env-file=...`); no doppler wrapper needed since
    # Doppler's decommission (2026-07-03).
    result["stage"] = "db_apply"
    _progress("db_apply_start", args.task_id, f"table={table} slug={slug}")
    cmd = [VENV_PY, DB_APPLY, "--table", table, "--slug", slug,
           "--fields-file", fields_file,
           "--allowed-cols", ",".join(cfg.allowed_cols),
           "--db-url-env", ",".join(cfg.db_url_env)]
    if args.dry_run:
        cmd.append("--dry-run")
    try:
        da = run(cmd, timeout=120)
    except Exception as e:
        result["error"] = f"db_apply invocation failed: {e}"
        return out(result, 3)
    da_json = None
    for line in reversed((da.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                da_json = json.loads(line); break
            except Exception:
                continue
    if da_json is None:
        result["error"] = f"db_apply produced no parseable JSON; stderr_tail={(da.stderr or '')[-600:]}"
        return out(result, 3)
    if not da_json.get("ok"):
        result["error"] = f"db_apply ok:false — {da_json.get('error')}"
        result["db_apply"] = da_json
        return out(result, 3)

    # 6. Single structured success result -------------------------------------
    result.update({
        "ok": True,
        "stage": "done",
        "columns_updated": da_json.get("columns_updated"),
        "dropped_unknown": da_json.get("dropped_unknown"),
        "aliased": da_json.get("aliased"),
        "backup_file": da_json.get("backup_file"),
        "content_len_before": da_json.get("content_len_before"),
        "content_len_after": da_json.get("content_len_after"),
        "id": da_json.get("id"),
        "title_after": da_json.get("title_after"),
    })

    # 6b. Self-attesting publish marker (2026-06-26) — durable proof the row was
    # written, consumed by db_closeout_actor.py so a hung executor (which never
    # posts the closeout / flips status) cannot strand a successfully-published
    # blog in 'in progress'. Best-effort: a failed marker must NOT fail the publish
    # (the row IS already written). SKIPPED on --dry-run (nothing was written).
    if not args.dry_run:
        try:
            mdir = os.path.join(DELIVERABLES, args.task_id)
            os.makedirs(mdir, exist_ok=True)
            marker = {
                "task_id": args.task_id,
                "slug": slug,
                "table": table,
                "site": cfg.domain,
                "posts_id": da_json.get("id"),
                "title_after": da_json.get("title_after"),
                "content_len_after": da_json.get("content_len_after"),
                "backup_file": da_json.get("backup_file"),
                "live_url": result.get("live_url"),
                "ts": datetime.datetime.now().astimezone().isoformat(),
            }
            tmp = os.path.join(mdir, "publish_result.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(marker, fh, indent=2)
            os.replace(tmp, os.path.join(mdir, "publish_result.json"))
            result["publish_marker"] = "written"
        except Exception as e:
            result["publish_marker"] = f"marker write failed (non-fatal): {e}"

    # 7. Deterministic deliverable attach (Agent A4, 2026-06-24) — preserve the published
    # content snapshot to the STABLE dir + attach it to the task so the reviewer on the
    # Windows PC can see the exact content (the live URL is the primary review target, but
    # the workdir is ephemeral and cross-machine-invisible). SKIPPED on --dry-run. Best-effort:
    # a failed attach must NOT fail the publish (the row IS already written) — recorded under
    # result["attach"] for audit.
    if not args.dry_run:
        try:
            files = [fields_file]
            for sidecar in ("summary.txt", "sources.txt"):
                sp = os.path.join(workdir, sidecar)
                if os.path.isfile(sp) and os.path.getsize(sp) > 0:
                    files.append(sp)
            ad = run(["python3", os.path.join(SCRIPTS, "attach_deliverable.py"),
                      "--task-id", args.task_id, *files], timeout=180)
            ad_json = None
            for line in reversed((ad.stdout or "").strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        ad_json = json.loads(line); break
                    except Exception:
                        continue
            result["attach"] = ad_json or {"ok": False, "error": (ad.stderr or ad.stdout or "")[-300:]}
        except Exception as e:
            result["attach"] = {"ok": False, "error": f"attach step raised: {e}"}

    return out(result, 0)


if __name__ == "__main__":
    sys.exit(main())
