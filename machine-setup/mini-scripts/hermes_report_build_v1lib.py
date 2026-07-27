#!/usr/bin/env python3
"""
hermes_report_build.py — build the two-table Hermes status email (HTML + text).

Colin's spec (2026-07-02): the 6-hourly report shows tasks as
    project | list | taskname (link) | status
where `status` is the live ClickUp status (NOT a Hermes-internal status) and
`taskname` links to the ClickUp task. Two SEPARATE tables:

  1. HERMES LIST  — the current board/queue: every task on Hermes's plate right
                    now (from ~/.hermes/scripts/queue_snapshot.json).
  2. WORK LIST    — what Hermes actually did in the trailing window: every task
                    touched (claimed/worked/commented/PR'd/shipped), derived from
                    the executor/reconciler cron-output briefs in the window.

The tables are built DETERMINISTICALLY here (not by an LLM subagent) — this
replaces the old "Subagent B" scan that was documented to silently report zero
while real work was happening (brain: 2026-07-02 hermes-self-report false-STALLED).

project/list/status/url for each task id are resolved once via the portable
clickup.mjs CLI (`task <id> --json --compact`), cached across both tables.

The diagnostic header (health / model / auth / work-stoppage verdict /
needs-attention) is still supplied by the skill's mechanical-scan subagents and
passed in via --header-file (JSON). This script owns ONLY the tables + final
HTML/text assembly + count reconciliation.

DEPRECATED (v1 table renderer superseded by hermes_report_build.py's v2 card layout).
This module is still imported as a data-collection LIBRARY by hermes_report_build.py; only its
standalone __main__ path is deprecated/neutralized below.

Outputs (paths printed as JSON on stdout for the skill to pipe to Postmark):
  --out-html  final HTML body   (default /tmp/hermes_report_v1lib_DEPRECATED.html)
  --out-text  final text body   (default /tmp/hermes_report_v1lib_DEPRECATED.txt)
and a machine-readable summary block: counts the skill's reconciliation gate uses
(briefs_scanned, task_ids_found, hermes_list_n, work_list_n, work_completed).
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERMES = os.path.expanduser("~/.hermes")
SNAPSHOT = os.path.join(HERMES, "scripts", "queue_snapshot.json")
BRIEFS_DIR = os.path.join(HERMES, "cron", "output")
JOBS_JSON = os.path.join(HERMES, "cron", "jobs.json")
NODE_CANDIDATES = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
)
CLICKUP_CLI_CANDIDATES = (
    os.path.join(HERMES, "scripts", "clickup", "clickup.mjs"),
    os.path.expanduser("~/.codex/skills/clickup/clickup.mjs"),
    os.path.expanduser("~/.claude/skills/clickup/clickup.mjs"),
)
# Modern ClickUp ids are base-36 and contain at least one letter after the
# workspace prefix. Requiring that letter keeps numeric GitHub check/artifact
# ids such as ``8612082831`` out of the ClickUp resolver.
TASK_ID_RE = re.compile(
    r"\b86(?=[0-9a-z]{7,10}\b)(?=[0-9a-z]*[a-z])[0-9a-z]{7,10}\b"
)
RESPONSE_SPLIT_RE = re.compile(r"\n#+\s*Response\b", re.I)
PASS_RE = re.compile(r"\bPASS\b", re.I)
FAIL_RE = re.compile(r"\bFAIL\b", re.I)

# Only these crons actually ACT on tasks (claim/work/PR/close). The poll-gate,
# reconciler, usage-alert, and triage crons merely ENUMERATE the board — their
# briefs list every task id and would flood the work list with tasks Hermes
# never touched. Resolved by name (not id) so a job-id change doesn't break it.
WORK_CRON_NAMES = {
    "clickup-executor",
    "clickup-executor-2",
    "clickup-closeout-actor",
    "hermes-pr-validate",
}

COMPLETE_STATUSES = {"complete", "closed", "done"}


def _node_bin():
    configured = os.environ.get("NODE_BIN")
    if configured:
        return configured
    discovered = shutil.which("node")
    if discovered:
        return discovered
    for candidate in NODE_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _clickup_cli():
    configured = os.environ.get("CLICKUP_CLI")
    if configured:
        return os.path.expanduser(configured)
    for candidate in CLICKUP_CLI_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _resolution_failure(task_id, reason):
    return {
        "id": task_id,
        "project": "—",
        "list": "—",
        "name": task_id,
        "url": f"https://app.clickup.com/t/{task_id}",
        "status": "resolution failed",
        "date_closed": None,
        "date_done": None,
        "status_type": "",
        "resolution_error": reason,
    }


def _resolve_task(task_id, cache):
    """Resolve one ClickUp task, preserving a visible error row on failure."""
    if task_id in cache:
        return cache[task_id]
    node_bin = _node_bin()
    clickup_cli = _clickup_cli()
    if not node_bin:
        row = _resolution_failure(task_id, "node executable not found")
        cache[task_id] = row
        return row
    if not clickup_cli:
        row = _resolution_failure(task_id, "clickup.mjs not found")
        cache[task_id] = row
        return row

    try:
        out = subprocess.run(
            [node_bin, clickup_cli, "task", task_id, "--json", "--compact"],
            capture_output=True, text=True, timeout=45,
        )
        if out.returncode == 0 and out.stdout.strip():
            d = json.loads(out.stdout)
            folder = d.get("folder") or d.get("project") or {}
            lst = d.get("list") or {}
            st = d.get("status")
            status = st.get("status") if isinstance(st, dict) else (st or "")
            row = {
                "id": task_id,
                "project": (folder.get("name") or "—") if isinstance(folder, dict) else "—",
                "list": (lst.get("name") or "—") if isinstance(lst, dict) else "—",
                "name": d.get("name") or task_id,
                "url": d.get("url") or f"https://app.clickup.com/t/{task_id}",
                "status": status or "unknown",
                "status_type": st.get("type", "") if isinstance(st, dict) else "",
                "date_closed": d.get("date_closed"),
                "date_done": d.get("date_done"),
                "resolution_error": None,
            }
        else:
            detail = (out.stderr or out.stdout or f"exit {out.returncode}").strip().splitlines()
            row = _resolution_failure(task_id, detail[-1] if detail else f"exit {out.returncode}")
    except Exception as exc:
        row = _resolution_failure(task_id, f"{type(exc).__name__}: {exc}")
    cache[task_id] = row
    return row


def build_hermes_list(cache):
    """Current board/queue from the snapshot, resolved to live project/list/status."""
    try:
        snap = json.load(open(SNAPSHOT))
    except Exception as e:
        return [], {"error": f"snapshot unreadable: {e}"}, None
    rows = []
    for t in snap.get("tasks", []):
        tid = t.get("id")
        r = _resolve_task(tid, cache) if tid else None
        if r is None or r.get("resolution_error"):
            # Preserve useful snapshot fields while retaining the resolution
            # error so the report can flag degraded collection.
            resolution_error = (r or {}).get("resolution_error")
            r = {
                "id": tid,
                "project": "—",
                "list": t.get("list") or "—",
                "name": t.get("name") or tid,
                "url": t.get("url") or f"https://app.clickup.com/t/{tid}",
                "status": t.get("status") or "unknown",
                "date_closed": t.get("date_closed"),
                "date_done": t.get("date_done"),
                "status_type": "",
                "resolution_error": resolution_error,
            }
        rows.append(r)
    meta = {
        "generated": snap.get("generated_at_human"),
        "ready": snap.get("agent_ready_count", 0),
    }
    return rows, meta, snap


def _work_cron_dirs():
    """Resolve work cron names to their output directories."""
    try:
        j = json.load(open(JOBS_JSON))
    except Exception:
        return []
    jobs = j if isinstance(j, list) else j.get("jobs", j)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    dirs = []
    for job in jobs:
        if isinstance(job, dict) and job.get("name") in WORK_CRON_NAMES and job.get("id"):
            d = os.path.join(BRIEFS_DIR, job["id"])
            if os.path.isdir(d):
                dirs.append((job["name"], d))
    return dirs


def _validator_pass_ids(response):
    """Extract task ids explicitly associated with PASS on the same line.

    Validator summaries use forms such as ``PASS → id1, id2`` and
    ``id — PASS``. Restricting attribution to the same line avoids counting
    FAIL ids from mixed PASS/FAIL summaries.
    """
    passed = set()
    for line in response.splitlines():
        if not PASS_RE.search(line):
            continue
        if FAIL_RE.search(line):
            for segment in re.split(r"\bFAIL\b", line, flags=re.I):
                if PASS_RE.search(segment):
                    passed.update(TASK_ID_RE.findall(segment))
            continue
        passed.update(TASK_ID_RE.findall(line))
    return passed


def _closed_in_window(row, cutoff_ms):
    status = str(row.get("status") or "").lower()
    if status not in COMPLETE_STATUSES:
        return False
    try:
        return int(row.get("date_closed") or 0) >= cutoff_ms
    except (TypeError, ValueError):
        return False


def build_work_list(window_min, cache):
    """Tasks Hermes actually acted on in the trailing window.

    Scans ONLY the work-cron briefs (executors / closeout-actor / pr-validate)
    and ONLY each brief's `## Response` section — the part the agent wrote about
    what it did. Everything above Response is the embedded skill prompt, which
    contains example task ids and the full candidate-board scan (pure noise).
    """
    cutoff = time.time() - window_min * 60
    briefs = []
    for job_name, d in _work_cron_dirs():
        for f in os.listdir(d):
            if not f.endswith(".md"):
                continue
            p = os.path.join(d, f)
            try:
                if os.path.getmtime(p) >= cutoff:
                    briefs.append((job_name, p))
            except OSError:
                pass
    ids = []
    seen = set()
    validator_pass_ids = set()
    for job_name, p in briefs:
        try:
            with open(p, errors="replace") as brief_file:
                txt = brief_file.read()
        except OSError:
            continue
        parts = RESPONSE_SPLIT_RE.split(txt, maxsplit=1)
        resp = parts[-1] if len(parts) > 1 else ""  # nothing before a Response = nothing done
        if job_name == "hermes-pr-validate":
            validator_pass_ids.update(_validator_pass_ids(resp))
        for m in TASK_ID_RE.findall(resp):
            if m not in seen:
                seen.add(m)
                ids.append(m)
    rows = []
    for tid in ids:
        r = _resolve_task(tid, cache)
        if r is not None:
            rows.append(r)
    unresolved_ids = [r["id"] for r in rows if r.get("resolution_error")]
    completed_ids = [
        r["id"] for r in rows
        if r["id"] in validator_pass_ids and _closed_in_window(r, int(cutoff * 1000))
    ]
    counts = {
        "briefs_scanned": len(briefs),
        "task_ids_found": len(ids),
        "resolved": len(rows) - len(unresolved_ids),
        "unresolved": len(unresolved_ids),
        "unresolved_task_ids": unresolved_ids,
        "completed": len(completed_ids),
        "completed_task_ids": completed_ids,
    }
    return rows, counts


# ---------- rendering ----------

def _status_color(status):
    s = status.lower()
    if s in ("complete", "closed", "done"):
        return "#0a7d33"
    if s in ("in review", "ready for review"):
        return "#0b69c7"
    if s == "in progress":
        return "#b15c00"
    if s in ("needs human", "blocked"):
        return "#b00020"
    return "#555"


def render_html_table(rows):
    if not rows:
        return '<p style="color:#777;margin:4px 0 16px">none in the last 6h</p>'
    out = [
        '<table cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;width:100%;font-size:13px;margin:4px 0 20px">',
        '<tr style="text-align:left;color:#666;border-bottom:2px solid #ddd">'
        '<th style="padding:6px 10px 6px 0">Project</th>'
        '<th style="padding:6px 10px">List</th>'
        '<th style="padding:6px 10px">Task</th>'
        '<th style="padding:6px 0 6px 10px">Status</th></tr>',
    ]
    for r in rows:
        name = html.escape(r["name"])
        url = html.escape(r["url"], quote=True)
        out.append(
            '<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:6px 10px 6px 0;color:#333;white-space:nowrap">{html.escape(r["project"])}</td>'
            f'<td style="padding:6px 10px;color:#333;white-space:nowrap">{html.escape(r["list"])}</td>'
            f'<td style="padding:6px 10px"><a href="{url}" style="color:#0b57d0;text-decoration:none">{name}</a></td>'
            f'<td style="padding:6px 0 6px 10px;color:{_status_color(r["status"])};'
            f'white-space:nowrap;font-weight:600">{html.escape(r["status"])}</td>'
            '</tr>'
        )
    out.append("</table>")
    return "\n".join(out)


def render_text_table(rows):
    if not rows:
        return "  none in the last 6h"
    lines = []
    for r in rows:
        lines.append(
            f"  {r['project']}  |  {r['list']}  |  {r['name']}  |  {r['status']}\n"
            f"    {r['url']}"
        )
    return "\n".join(lines)


def build_html(header, hermes_rows, hermes_meta, work_rows):
    h = header
    def esc(x):
        return html.escape(str(x)) if x is not None else ""
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'color:#1a1a1a;max-width:820px;margin:0 auto;line-height:1.45">',
        f'<h2 style="margin:0 0 4px;font-size:18px">Hermes status — {esc(h.get("when"))}, trailing 6h</h2>',
        '<table cellpadding="0" cellspacing="0" border="0" style="font-size:13px;margin:8px 0 20px">'
        f'<tr><td style="padding:2px 12px 2px 0;color:#666">HEALTH</td><td>{esc(h.get("health"))}</td></tr>'
        f'<tr><td style="padding:2px 12px 2px 0;color:#666">MODEL</td><td>{esc(h.get("model"))}</td></tr>'
        f'<tr><td style="padding:2px 12px 2px 0;color:#666">AUTH</td><td>{esc(h.get("auth"))}</td></tr>'
        '</table>',
        f'<h3 style="margin:18px 0 2px;font-size:15px">Hermes list '
        f'<span style="font-weight:400;color:#888;font-size:12px">— current board / queue '
        f'({len(hermes_rows)} tasks, {esc(hermes_meta.get("ready"))} agent-ready · snapshot {esc(hermes_meta.get("generated"))})</span></h3>',
        render_html_table(hermes_rows),
        f'<h3 style="margin:18px 0 2px;font-size:15px">Work list '
        f'<span style="font-weight:400;color:#888;font-size:12px">— what Hermes did in ClickUp this window '
        f'({len(work_rows)} tasks)</span></h3>',
        render_html_table(work_rows),
    ]
    ws = h.get("work_stoppage")
    if ws:
        parts.append(
            f'<p style="margin:16px 0 4px;font-size:13px;color:#333">'
            f'<b>WORK STOPPAGE:</b> {esc(ws)}</p>'
        )
    na = h.get("needs_attention")
    if na:
        parts.append(
            f'<div style="margin:8px 0;padding:10px 12px;background:#fff6f6;'
            f'border-left:3px solid #b00020;font-size:13px">'
            f'<b>NEEDS ATTENTION:</b><br>{esc(na).replace(chr(10), "<br>")}</div>'
        )
    parts.append(
        '<p style="margin:20px 0 0;font-size:11px;color:#999">'
        'Read-only status report. It does not fix anything — see ignite-babysit-hermes for that.</p>'
        '</div>'
    )
    return "\n".join(parts)


def build_text(header, hermes_rows, hermes_meta, work_rows):
    h = header
    lines = [
        f'Hermes status — {h.get("when","")}, trailing 6h',
        "",
        f'HEALTH: {h.get("health","")}',
        f'MODEL:  {h.get("model","")}',
        f'AUTH:   {h.get("auth","")}',
        "",
        f'HERMES LIST — current board / queue ({len(hermes_rows)} tasks, '
        f'{hermes_meta.get("ready",0)} agent-ready · snapshot {hermes_meta.get("generated","")})',
        "  (project | list | task | status)",
        render_text_table(hermes_rows),
        "",
        f'WORK LIST — what Hermes did in ClickUp this window ({len(work_rows)} tasks)',
        "  (project | list | task | status)",
        render_text_table(work_rows),
        "",
    ]
    if h.get("work_stoppage"):
        lines.append(f'WORK STOPPAGE: {h["work_stoppage"]}')
    if h.get("needs_attention"):
        lines.append(f'NEEDS ATTENTION: {h["needs_attention"]}')
    lines += [
        "",
        "(Read-only status report. It does not fix anything — see ignite-babysit-hermes for that.)",
    ]
    return "\n".join(lines)


def main():
    print(
        "WARNING: hermes_report_build_v1lib.py is the DEPRECATED v1 table renderer; the live "
        "report is hermes_report_build.py (v2 cards). This standalone run will NOT overwrite the "
        "live /tmp/hermes_report.html.",
        file=sys.stderr,
    )
    p = argparse.ArgumentParser()
    p.add_argument("--window-min", type=int, default=360)
    p.add_argument("--header-file", help="JSON: when, health, model, auth, work_stoppage, needs_attention")
    p.add_argument("--out-html", default="/tmp/hermes_report_v1lib_DEPRECATED.html")
    p.add_argument("--out-text", default="/tmp/hermes_report_v1lib_DEPRECATED.txt")
    args = p.parse_args()

    header = {}
    if args.header_file and os.path.exists(args.header_file):
        try:
            header = json.load(open(args.header_file))
        except Exception as e:
            print(f"WARN: header-file unreadable ({e}); using empty header", file=sys.stderr)

    cache = {}
    hermes_rows, hermes_meta, _snap = build_hermes_list(cache)
    work_rows, work_counts = build_work_list(args.window_min, cache)

    html_body = build_html(header, hermes_rows, hermes_meta, work_rows)
    text_body = build_text(header, hermes_rows, hermes_meta, work_rows)

    with open(args.out_html, "w") as f:
        f.write(html_body)
    with open(args.out_text, "w") as f:
        f.write(text_body)

    summary = {
        "out_html": args.out_html,
        "out_text": args.out_text,
        "hermes_list_n": len(hermes_rows),
        "work_list_n": len(work_rows),
        "work_completed": work_counts["completed"],
        "briefs_scanned": work_counts["briefs_scanned"],
        "task_ids_found": work_counts["task_ids_found"],
        "snapshot_generated": hermes_meta.get("generated"),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
