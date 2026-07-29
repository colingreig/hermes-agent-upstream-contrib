#!/usr/bin/env python3
"""
hermes_report_build.py — deterministic Hermes status email (HTML + text).

The mobile-first v2 renderer reuses the frozen v1 data-collection layer
(queue snapshot + work-cron briefs + live clickup.mjs resolution) and adds a
spend/model section sourced from ~/.hermes/logs/writer-served.jsonl.

Why a rewrite of the rendering layer: v1's two 4-column HTML tables
(project | list | task | status) are unreadable in a mobile mail client —
that's the defect this candidate fixes. v2 restructures the SAME underlying
data into a mobile-first single-column layout:

  1. Headline banner (mirrors the subject)
  2. ACTION REQUIRED (blocked/needs-human only)
  3. WATCH LIST (stale in-progress only)
  4. SYSTEM SIGNALS
  5. SCOREBOARD (ready/in-progress/in-review/blocked/validator-completed, lanes)
  6. MODEL & SPEND (from the writer-served ledger: cost, per-provider, drift)
  7. ROSTER (work list + queue list, bounded and moved to the bottom)

v1's collection functions (_resolve_task, build_hermes_list, build_work_list,
_work_cron_dirs) are reused via `import hermes_report_build_v1lib as v1` — that
lib is a frozen copy of the original v1 data layer, untouched and guarding its
own execution under `if __name__ == "__main__"`, so importing it does not run
its main(). (This file is installed AS hermes_report_build.py at cutover, so it
must import the collectors from the v1lib copy, not from itself.)

Outputs (paths printed as JSON on stdout, v1-compatible keys + new ones):
  --out-html     final HTML body     (default /tmp/hermes_report.html)
  --out-text     final text body     (default /tmp/hermes_report.txt)
  --out-subject  suggested subject   (default /tmp/hermes_report_subject.txt)
"""
import argparse
import collections
import datetime
import html
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import hermes_report_build_v1lib as v1  # noqa: E402  (reuse v1's data-collection layer)

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    LOCAL_TZ = None

HERMES = os.path.expanduser("~/.hermes")
SERVED_LEDGER_DEFAULT = os.path.join(HERMES, "logs", "writer-served.jsonl")

STUCK_IN_PROGRESS_HOURS = 2.0
MAX_SECTION_ROWS = 10


# ---------- spend / model ledger ----------

def _parse_ts(ts):
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def _local_day(dt):
    if dt is None:
        return None
    if LOCAL_TZ is not None:
        dt = dt.astimezone(LOCAL_TZ)
    return dt.date()


def load_served_ledger(path, window_min):
    """Parse writer-served.jsonl. Returns (window_rows, today_rows, previous_window_rows, error)."""
    if not path or not os.path.exists(path):
        return [], [], [], f"ledger not found: {path}"
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(minutes=window_min)
    previous_cutoff = cutoff - datetime.timedelta(minutes=window_min)
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                dt = _parse_ts(rec.get("ts", ""))
                rec["_dt"] = dt
                rows.append(rec)
    except Exception as e:
        return [], [], [], f"ledger unreadable: {e}"

    if not rows:
        return [], [], [], None

    window_rows = [r for r in rows if r["_dt"] is not None and r["_dt"] >= cutoff]
    previous_window_rows = [
        r for r in rows
        if r["_dt"] is not None and previous_cutoff <= r["_dt"] < cutoff
    ]

    today_local = _local_day(now)
    today_rows = [r for r in rows if _local_day(r["_dt"]) == today_local]

    return window_rows, today_rows, previous_window_rows, None


def summarize_spend(window_rows, today_rows, previous_window_rows):
    total_cost = sum(float(r.get("cost_usd") or 0) for r in window_rows)
    today_cost = sum(float(r.get("cost_usd") or 0) for r in today_rows)
    previous_window_cost = sum(float(r.get("cost_usd") or 0) for r in previous_window_rows)

    by_provider = collections.defaultdict(lambda: {"n": 0, "cost": 0.0, "degraded": 0})
    for r in window_rows:
        prov = r.get("served_provider") or r.get("served_model") or "unknown"
        by_provider[prov]["n"] += 1
        by_provider[prov]["cost"] += float(r.get("cost_usd") or 0)
        if r.get("degraded"):
            by_provider[prov]["degraded"] += 1
    provider_rows = sorted(
        ({"provider": k, **v} for k, v in by_provider.items()),
        key=lambda x: x["cost"], reverse=True,
    )

    drift = [
        r for r in window_rows
        if r.get("expected_primary_model") and r.get("served_model")
        and r.get("expected_primary_model") != r.get("served_model")
    ]
    drift_targets = collections.Counter(r.get("served_model") for r in drift)
    top_drift_model = drift_targets.most_common(1)[0][0] if drift_targets else None

    return {
        "total_cost": total_cost,
        "today_cost": today_cost,
        "previous_window_cost": previous_window_cost,
        "cost_delta": total_cost - previous_window_cost,
        "provider_rows": provider_rows,
        "providers_n": len(provider_rows),
        "runs_n": len(window_rows),
        "drift_n": len(drift),
        "top_drift_model": top_drift_model,
    }


def _cost_display(spend):
    """Render spend['total_cost'] for the subject/headline lines.

    A served-ledger READ FAILURE (spend['error'] set, total_cost=None) must
    never render as "$0.00" — that is indistinguishable from a real quiet
    period with zero spend and hides the failure from Colin. Only a genuine
    zero (ledger read fine, nothing served in the window) prints as $0.00.
    """
    if spend.get("error"):
        return "spend UNKNOWN (ledger unreadable)"
    return f"${(spend.get('total_cost') or 0.0):.2f}"


# ---------- alerts ----------

def _epoch_ms_to_dt(ms_str):
    try:
        return datetime.datetime.fromtimestamp(int(ms_str) / 1000.0, tz=datetime.timezone.utc)
    except Exception:
        return None


def _health_identity(name, detail):
    """Stable identity for semantically identical health signals."""
    detail_text = str(detail or "").strip().lower()
    text = f"{name or ''} {detail_text}".strip().lower()
    if "stalled" in text and ("work stoppage" in text or detail_text == "stalled"):
        return "work-stoppage:stalled"
    if "unknown" in text and "work stoppage" in text:
        return "work-stoppage:unknown"
    return " ".join(text.split())


def build_alerts(hermes_rows, snap_tasks_by_id, header, now=None):
    """Build mutually exclusive, deduplicated task and health alert cards."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    task_alerts = {}

    for r in hermes_rows:
        tid = r.get("id")
        snap_t = snap_tasks_by_id.get(tid) or {}
        status = (r.get("status") or "").lower()
        tags = snap_t.get("tags") or []

        # A task is exactly one task-alert category. Human-gated wins over stale
        # in-progress, and duplicate queue rows collapse by ClickUp task id.
        is_blocked = "blocked" in status or "needs human" in status or "needs-human" in tags
        if is_blocked:
            task_alerts[tid] = {
                "kind": "blocked",
                "id": tid,
                "name": r.get("name"),
                "url": r.get("url"),
                "detail": "Awaiting you",
                "sub": r.get("list"),
            }
        elif "in progress" in status:
            du = snap_t.get("date_updated")
            dt = _epoch_ms_to_dt(du) if du else None
            if dt is not None:
                age_h = (now - dt).total_seconds() / 3600.0
                if age_h >= STUCK_IN_PROGRESS_HOURS:
                    task_alerts[tid] = {
                        "kind": "stuck",
                        "id": tid,
                        "name": r.get("name"),
                        "url": r.get("url"),
                        "detail": f"In progress {age_h:.0f}h, no update",
                        "sub": r.get("list"),
                    }

    health_alerts = {}
    work_stoppage = (header or {}).get("work_stoppage") or ""
    if work_stoppage and not work_stoppage.strip().endswith("-> OK") and "-> ok" not in work_stoppage.lower():
        alert = {
            "kind": "health",
            "name": "Work stoppage signal",
            "url": None,
            "detail": work_stoppage,
            "sub": "Health scan",
        }
        health_alerts[_health_identity(alert["name"], alert["detail"])] = alert

    needs_attention = (header or {}).get("needs_attention") or ""
    for signal in needs_attention.split(";"):
        signal = signal.strip()
        if signal:
            alert = {
                "kind": "health",
                "name": "Needs attention",
                "url": None,
                "detail": signal,
                "sub": "Health scan",
            }
            health_alerts.setdefault(_health_identity(alert["name"], alert["detail"]), alert)

    return list(task_alerts.values()) + list(health_alerts.values())


# ---------- scoreboard ----------

def build_scoreboard(hermes_rows, snap_tasks, hermes_meta, work_completed):
    statuses = collections.Counter((r.get("status") or "").lower() for r in hermes_rows)
    in_progress = statuses.get("in progress", 0)
    in_review = statuses.get("in review", 0) + statuses.get("ready for review", 0)
    blocked = statuses.get("blocked", 0) + statuses.get("needs human", 0)

    lanes = collections.Counter()
    for t in snap_tasks:
        for tag in (t.get("tags") or []):
            if tag == "lane:code":
                lanes["code"] += 1
            elif tag == "lane:content":
                lanes["content"] += 1

    return {
        "ready": hermes_meta.get("ready", 0),
        "in_progress": in_progress,
        "in_review": in_review,
        "blocked": blocked,
        "validator_completed_window": work_completed,
        "lane_code": lanes.get("code", 0),
        "lane_content": lanes.get("content", 0),
    }


# ---------- subject / headline ----------

def summarize_alerts(alerts):
    counts = collections.Counter(a.get("kind") for a in alerts)
    return {
        "watch_list": counts.get("stuck", 0),
        "action_required": counts.get("blocked", 0),
        "system_signals": counts.get("health", 0),
        "alerts_n": len(alerts),
    }


def _alert_count_text(alert_summary):
    return (
        f"{alert_summary['action_required']} action required · "
        f"{alert_summary['watch_list']} watch list · "
        f"{alert_summary['system_signals']} system signals"
    )


def build_subject(scoreboard, spend, alert_summary):
    completed = scoreboard["validator_completed_window"]
    subj = (
        f"Hermes: {completed} validator-completed · {_alert_count_text(alert_summary)} · "
        f"{_cost_display(spend)} · {scoreboard['ready']} ready"
    )
    return subj


def build_headline_emoji_text(scoreboard, spend, alert_summary):
    alerts_n = alert_summary["alerts_n"]
    dot = "🟢" if alerts_n == 0 else "⚠️"
    completed = scoreboard["validator_completed_window"]
    return (
        f"{dot} {completed} validator-completed · "
        f"{'⚠️' if alert_summary['action_required'] else '✓'} "
        f"{alert_summary['action_required']} action required · "
        f"{'⚠️' if alert_summary['watch_list'] else '✓'} "
        f"{alert_summary['watch_list']} watch list · "
        f"{'⚠️' if alert_summary['system_signals'] else '✓'} "
        f"{alert_summary['system_signals']} system signals · "
        f"💸 {_cost_display(spend)} · {scoreboard['ready']} ready"
    )


def add_resolution_health(header, work_counts):
    """Turn partial/total task-resolution loss into a visible health signal."""
    unresolved = int(work_counts.get("unresolved") or 0)
    if unresolved <= 0:
        return dict(header or {})
    found = int(work_counts.get("task_ids_found") or 0)
    resolved = int(work_counts.get("resolved") or 0)
    signal = (
        f"ClickUp work-card resolution degraded: {resolved}/{found} resolved; "
        f"{unresolved} unresolved"
    )
    merged = dict(header or {})
    existing = str(merged.get("needs_attention") or "").strip()
    merged["needs_attention"] = f"{existing}; {signal}".strip("; ") if existing else signal
    return merged


def _bounded(rows):
    rows = list(rows or [])
    return {"rows": rows[:MAX_SECTION_ROWS], "total": len(rows), "shown": min(len(rows), MAX_SECTION_ROWS)}


def build_report_view_model(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min):
    """Single structured report model consumed by subject, body, and JSON summary."""
    h = header or {}
    action_required = [a for a in alerts if a.get("kind") == "blocked"]
    watch_list = [a for a in alerts if a.get("kind") == "stuck"]
    system_signals = [a for a in alerts if a.get("kind") == "health"]
    sections = {
        "action_required": {"title": "Action required", "items": action_required},
        "watch_list": {"title": "Watch list", "items": watch_list},
        "system_signals": {"title": "System signals", "items": system_signals},
        "what_hermes_did": {"title": "What Hermes did", **_bounded(work_rows)},
        "queue": {"title": "Queue", **_bounded(hermes_rows)},
    }
    alert_summary = {
        "action_required": len(action_required),
        "watch_list": len(watch_list),
        "system_signals": len(system_signals),
        "alerts_n": len(action_required) + len(watch_list) + len(system_signals),
    }
    subject = build_subject(scoreboard, spend, alert_summary)
    return {
        "header": h,
        "scoreboard": scoreboard,
        "spend": spend,
        "sections": sections,
        "counts": alert_summary,
        "hermes_meta": hermes_meta or {},
        "window_min": window_min,
        "subject": subject,
        "headline": build_headline_emoji_text(scoreboard, spend, alert_summary),
    }


# ---------- work-stoppage verdict (deterministic; NOT delegated to the cron LLM) ----------
#
# RC4 postmortem (2026-07-25 cutover): the STALLED/OK/UNKNOWN verdict used to
# be entirely prose-computed by a cron subagent (hermes-self-report SKILL.md
# "Subagent C"). Two failures compounded into a false STALLED headline on
# every run since cutover:
#   1. That subagent's queue-snapshot check shelled out to
#      `~/.hermes/hermes-agent/venv/bin/python3.11` — a path retired by the
#      2026-07-19 migration to `~/.hermes/releases/*` + `runtime-current`. The
#      command errored on every invocation (confirmed in errors.log
#      2026-07-25 00:07:42: "No such file or directory").
#   2. A cheap cron model (gpt-5.4-mini) treated the resulting silence/failure
#      as `live_claims==0` instead of "unknown," which alone satisfies the
#      STALLED condition even while work is actively in flight.
# This is the same class of bug that already forced the queue/work-card lists
# off LLM narration and onto this script on 2026-07-02 (see the "Subagent
# B — DEPRECATED" note in SKILL.md). The fix is the same: compute the verdict
# here, deterministically, from raw counts, and fail to UNKNOWN — never to a
# false zero — when an input can't be read.

def _live_claims_count():
    """Returns (count, error). `count` is None (not 0) when claim-store data
    could not be read — a false zero is exactly what produces a false STALLED
    verdict, so this fails to "unknown," not to zero."""
    try:
        import claim_store
    except Exception as e:
        return None, f"claim_store import failed: {e}"
    try:
        return len(claim_store.list_live()), None
    except Exception as e:
        return None, f"claim_store.list_live() failed: {e}"


def compute_work_stoppage(ready, work_completed, in_progress, work_counts):
    """Deterministic replacement for the old prose-computed WORK STOPPAGE
    CHECK. Mirrors SKILL.md's documented rule exactly:

      STALLED iff completed==0 AND ready>0 AND (live_claims==0 OR in_progress>live_claims)

    but only after the reconciliation gate: if in_progress>0 while the
    trailing-window work-scan itself found nothing (briefs_scanned==0,
    task_ids_found==0, or tasks_resolved==0), or the live claim count could
    not be determined at all, the verdict is UNKNOWN, not STALLED.
    """
    briefs_scanned = int(work_counts.get("briefs_scanned") or 0)
    task_ids_found = int(work_counts.get("task_ids_found") or 0)
    tasks_resolved = int(work_counts.get("resolved") or 0)

    live_claims, claims_err = _live_claims_count()
    claims_unknown = live_claims is None

    reconciliation_failed = (
        in_progress > 0
        and (briefs_scanned == 0 or task_ids_found == 0 or tasks_resolved == 0)
    )

    live_claims_display = "?" if claims_unknown else live_claims
    base = (
        f"ready={ready} completed={work_completed} in_progress={in_progress} "
        f"live_claims={live_claims_display}"
    )

    if reconciliation_failed or claims_unknown:
        reasons = []
        if reconciliation_failed:
            reasons.append(
                f"work evidence unresolved despite in_progress={in_progress} "
                f"(briefs_scanned={briefs_scanned}, task_ids_found={task_ids_found}, "
                f"tasks_resolved={tasks_resolved})"
            )
        if claims_unknown:
            reasons.append(f"live claim count unavailable ({claims_err})")
        return f"{base} -> UNKNOWN — {'; '.join(reasons)}"

    if work_completed == 0 and ready > 0 and (live_claims == 0 or in_progress > live_claims):
        return f"{base} -> STALLED"

    return f"{base} -> OK"


# ---------- HTML rendering ----------

def _esc(x):
    return html.escape(str(x)) if x is not None else ""


def render_html_view(model):
    h = model["header"]
    scoreboard = model["scoreboard"]
    spend = model["spend"]
    sections = model["sections"]
    hermes_meta = model["hermes_meta"]
    window_min = model["window_min"]
    subject_line = model["subject"]
    when = _esc(h.get("when", ""))
    window_h = window_min / 60.0
    window_h_str = f"{window_h:.0f}" if window_h == int(window_h) else f"{window_h:.1f}"

    css_body = (
        "margin:0;padding:0;background:#f2f2f2;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
    )
    css_container = (
        "max-width:600px;margin:0 auto;background:#ffffff;"
    )

    parts = []
    parts.append('<!DOCTYPE html>')
    parts.append('<html>')
    parts.append('<head>')
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append(f'<title>{_esc(subject_line)}</title>')
    parts.append('</head>')
    parts.append(f'<body style="{css_body}">')
    parts.append(f'<div style="{css_container}">')

    # 1. Headline banner
    banner_color = "#e8f5e9" if model["counts"]["alerts_n"] == 0 else "#fff3e0"
    border_color = "#2e7d32" if model["counts"]["alerts_n"] == 0 else "#e65100"
    headline = model["headline"]
    parts.append(
        f'<div style="background:{banner_color};border-bottom:3px solid {border_color};'
        f'padding:18px 20px;">'
        f'<div style="font-size:18px;font-weight:700;color:#1a1a1a;line-height:1.3">{_esc(headline)}</div>'
        f'<div style="font-size:13px;color:#666;margin-top:6px">'
        f'Hermes status · {when} · trailing {window_h_str}h</div>'
        '</div>'
    )

    parts.append('<div style="padding:20px">')

    # 2. Action required
    action_items = sections["action_required"]["items"]
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">Action required</h2>')
    if not action_items:
        parts.append(
            '<div style="padding:14px 16px;background:#e8f5e9;border-left:4px solid #2e7d32;'
            'border-radius:4px;font-size:14px;color:#1a1a1a;margin-bottom:24px">'
            'No blocked or needs-human tasks right now.</div>'
        )
    else:
        parts.append('<div style="margin-bottom:24px">')
        for a in action_items:
            parts.append(render_html_alert_card(a, "#b00020"))
        parts.append('</div>')

    # 3. Watch list
    watch_items = sections["watch_list"]["items"]
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">Watch list</h2>')
    if not watch_items:
        parts.append('<div style="font-size:13px;color:#888;margin-bottom:24px">No stale in-progress tasks.</div>')
    else:
        parts.append('<div style="margin-bottom:24px">')
        for a in watch_items:
            parts.append(render_html_alert_card(a, "#b15c00"))
        parts.append('</div>')

    # 4. System signals
    system_items = sections["system_signals"]["items"]
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">System signals</h2>')
    if not system_items:
        parts.append('<div style="font-size:13px;color:#888;margin-bottom:24px">No system signals.</div>')
    else:
        parts.append('<div style="margin-bottom:24px">')
        for a in system_items:
            parts.append(render_html_alert_card(a, "#666"))
        parts.append('</div>')

    # 5. Scoreboard
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">📊 Scoreboard</h2>')
    cells = [
        ("Ready", scoreboard["ready"], "#0b57d0"),
        ("In progress", scoreboard["in_progress"], "#b15c00"),
        ("Current in review", scoreboard["in_review"], "#0b69c7"),
        ("Blocked", scoreboard["blocked"], "#b00020"),
        ("Validator-completed (window)", scoreboard["validator_completed_window"], "#0a7d33"),
    ]
    parts.append(
        '<table cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:separate;'
        'border-spacing:6px 6px;margin:0 -6px 8px">'
    )
    for i in range(0, len(cells), 2):
        parts.append('<tr>')
        for label, val, color in cells[i:i + 2]:
            parts.append(
                '<td style="width:50%;background:#f7f7f8;border-radius:6px;padding:12px;text-align:center;'
                'vertical-align:top">'
                f'<div style="font-size:22px;font-weight:700;color:{color}">{_esc(val)}</div>'
                f'<div style="font-size:12px;color:#666;margin-top:2px">{_esc(label)}</div>'
                '</td>'
            )
        if len(cells[i:i + 2]) == 1:
            parts.append('<td style="width:50%"></td>')
        parts.append('</tr>')
    parts.append('</table>')
    parts.append(
        f'<div style="font-size:13px;color:#555;margin-bottom:24px">'
        f'Lanes — Code: {scoreboard["lane_code"]} · Content: {scoreboard["lane_content"]}</div>'
    )

    # 6. Model & spend
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">💸 Model &amp; spend</h2>')
    if spend.get("error"):
        parts.append(
            '<div style="padding:12px 14px;background:#fdecea;border:1px solid #b00020;'
            'border-radius:4px;font-size:14px;color:#b00020;margin-bottom:24px">'
            f'⚠️ Served ledger UNREADABLE ({_esc(str(spend["error"]))}) — spend this window is '
            'UNKNOWN, not $0.00.</div>'
        )
    elif spend.get("empty"):
        parts.append(
            '<div style="padding:12px 14px;background:#f7f7f8;border-radius:4px;font-size:14px;'
            'color:#666;margin-bottom:24px">No served-by receipts in the window.</div>'
        )
    else:
        cost_line = f'Total est. cost this window: <b>${spend["total_cost"]:.2f}</b>'
        delta = spend["cost_delta"]
        if delta > 0.0001:
            cost_line += f' &nbsp; <span style="color:#b00020">▲ ${delta:.2f} vs previous window</span>'
        elif delta < -0.0001:
            cost_line += f' &nbsp; <span style="color:#0a7d33">▼ ${abs(delta):.2f} vs previous window</span>'
        else:
            cost_line += ' &nbsp; <span style="color:#888">(flat vs previous window)</span>'
        parts.append(f'<div style="font-size:14px;color:#1a1a1a;margin-bottom:10px">{cost_line}</div>')
        parts.append(
            f'<div style="font-size:13px;color:#555;margin-bottom:10px">'
            f'Today so far: ${spend["today_cost"]:.2f}</div>'
        )

        parts.append('<div style="margin-bottom:10px">')
        for pr in spend["provider_rows"]:
            deg = f' · {pr["degraded"]} degraded' if pr["degraded"] else ''
            parts.append(
                '<div style="padding:10px 12px;background:#f7f7f8;border-radius:4px;margin-bottom:6px;'
                'font-size:13px;color:#1a1a1a">'
                f'<b>{_esc(pr["provider"])}</b> · {pr["n"]} runs · ${pr["cost"]:.2f}{deg}'
                '</div>'
            )
        parts.append('</div>')

        if spend["drift_n"] > 0:
            top = f' (mostly {_esc(spend["top_drift_model"])})' if spend["top_drift_model"] else ''
            parts.append(
                f'<div style="font-size:13px;color:#b15c00;margin-bottom:24px">'
                f'⚠️ {spend["drift_n"]} runs fell off the pinned model{top}</div>'
            )
        else:
            parts.append(
                '<div style="font-size:13px;color:#0a7d33;margin-bottom:24px">'
                '✓ All runs served on the pinned model.</div>'
            )

    # 7. Roster
    work_section = sections["what_hermes_did"]
    parts.append(
        f'<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">📋 What Hermes did '
        f'({work_section["total"]})</h2>'
    )
    parts.append(render_html_cards(work_section["rows"]))
    if work_section["shown"] < work_section["total"]:
        parts.append(f'<div style="font-size:12px;color:#888;margin-bottom:16px">Showing {work_section["shown"]} of {work_section["total"]}.</div>')

    queue_section = sections["queue"]
    parts.append(
        f'<h2 style="margin:24px 0 12px;font-size:16px;color:#1a1a1a">📋 Queue '
        f'({queue_section["total"]})</h2>'
    )
    parts.append(
        f'<div style="font-size:12px;color:#888;margin:-8px 0 12px">'
        f'{_esc(hermes_meta.get("ready"))} agent-ready · snapshot {_esc(hermes_meta.get("generated"))}</div>'
    )
    parts.append(render_html_cards(queue_section["rows"]))
    if queue_section["shown"] < queue_section["total"]:
        parts.append(f'<div style="font-size:12px;color:#888;margin-bottom:16px">Showing {queue_section["shown"]} of {queue_section["total"]}.</div>')

    parts.append(
        '<p style="margin:24px 0 0;font-size:11px;color:#999">'
        'Read-only status report. It does not fix anything — '
        'see ignite-babysit-hermes for that.</p>'
    )

    parts.append('</div>')  # padding div
    parts.append('</div>')  # container
    parts.append('</body>')
    parts.append('</html>')
    return "\n".join(parts)


def render_html_alert_card(a, color):
    name = _esc(a.get("name") or "")
    url = a.get("url")
    detail = _esc(a.get("detail") or "")
    sub = _esc(a.get("sub") or "")
    if url:
        title_html = (
            f'<a href="{_esc(url)}" style="color:#0b57d0;text-decoration:none;'
            f'font-weight:600;font-size:14px">{name}</a>'
        )
    else:
        title_html = f'<span style="font-weight:600;font-size:14px;color:#1a1a1a">{name}</span>'
    return (
        '<div style="padding:12px 14px;margin-bottom:8px;background:#fff6f6;'
        f'border-left:4px solid {color};border-radius:4px">'
        f'{title_html}'
        f'<div style="font-size:13px;color:{color};margin-top:4px;font-weight:600">{detail}</div>'
        + (f'<div style="font-size:12px;color:#888;margin-top:2px">{sub}</div>' if sub else '')
        + '</div>'
    )


def render_html(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min, subject_line=None):
    model = build_report_view_model(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min)
    if subject_line:
        model["subject"] = subject_line
    return render_html_view(model)


def render_html_cards(rows):
    if not rows:
        return '<div style="font-size:13px;color:#888;margin-bottom:16px">none in this window</div>'
    out = ['<div style="margin-bottom:16px">']
    for r in rows:
        name = _esc(r.get("name"))
        url = _esc(r.get("url", ""), )
        status = r.get("status") or "unknown"
        color = v1._status_color(status)
        project = _esc(r.get("project", "—"))
        lst = _esc(r.get("list", "—"))
        out.append(
            '<div style="padding:10px 12px;margin-bottom:6px;background:#fafafa;'
            'border:1px solid #eee;border-radius:4px">'
            f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;'
            f'background:{color};color:#fff;font-size:11px;font-weight:600;margin-bottom:6px">'
            f'{_esc(status)}</span><br>'
            f'<a href="{url}" style="color:#0b57d0;text-decoration:none;font-size:14px;'
            f'font-weight:600">{name}</a>'
            f'<div style="font-size:12px;color:#888;margin-top:2px">{project} · {lst}</div>'
            '</div>'
        )
    out.append('</div>')
    return "\n".join(out)


# ---------- text rendering ----------

def render_text_cards(rows):
    if not rows:
        return "  none in this window"
    lines = []
    for r in rows:
        lines.append(f'  [{r.get("status","unknown")}] {r.get("name")}')
        lines.append(f'    {r.get("project","—")} · {r.get("list","—")}')
        lines.append(f'    {r.get("url","")}')
    return "\n".join(lines)


def render_text_alerts(alerts):
    lines = []
    for a in alerts:
        lines.append(f'  - {a.get("name")}: {a.get("detail")}')
        if a.get("sub"):
            lines.append(f'    ({a["sub"]})')
        if a.get("url"):
            lines.append(f'    {a["url"]}')
    return lines


def build_text_view(model):
    h = model["header"]
    scoreboard = model["scoreboard"]
    spend = model["spend"]
    sections = model["sections"]
    hermes_meta = model["hermes_meta"]
    window_h = model["window_min"] / 60.0
    window_h_str = f"{window_h:.0f}" if window_h == int(window_h) else f"{window_h:.1f}"

    lines = [
        model["headline"],
        f'Hermes status · {h.get("when","")} · trailing {window_h_str}h',
        "",
        "=" * 40,
        "ACTION REQUIRED",
        "=" * 40,
    ]
    action_items = sections["action_required"]["items"]
    lines.extend(render_text_alerts(action_items) if action_items else ["  No blocked or needs-human tasks right now."])
    lines += ["", "=" * 40, "WATCH LIST", "=" * 40]
    watch_items = sections["watch_list"]["items"]
    lines.extend(render_text_alerts(watch_items) if watch_items else ["  No stale in-progress tasks."])
    lines += ["", "=" * 40, "SYSTEM SIGNALS", "=" * 40]
    system_items = sections["system_signals"]["items"]
    lines.extend(render_text_alerts(system_items) if system_items else ["  No system signals."])
    lines.append("")

    lines += [
        "=" * 40,
        "SCOREBOARD",
        "=" * 40,
        f'  Ready: {scoreboard["ready"]}   In progress: {scoreboard["in_progress"]}   '
        f'Current in review: {scoreboard["in_review"]}   Blocked: {scoreboard["blocked"]}   '
        f'Validator-completed (window): {scoreboard["validator_completed_window"]}',
        f'  Lanes - Code: {scoreboard["lane_code"]} · Content: {scoreboard["lane_content"]}',
        "",
    ]

    lines += ["=" * 40, "MODEL & SPEND", "=" * 40]
    if spend.get("error"):
        lines.append(f"  WARNING: served ledger UNREADABLE ({spend['error']}) — "
                      "spend this window is UNKNOWN, not $0.00.")
    elif spend.get("empty"):
        lines.append("  No served-by receipts in the window.")
    else:
        delta = spend["cost_delta"]
        if delta > 0.0001:
            delta_str = f'(up ${delta:.2f} vs previous window)'
        elif delta < -0.0001:
            delta_str = f'(down ${abs(delta):.2f} vs previous window)'
        else:
            delta_str = '(flat vs previous window)'
        lines.append(f'  Total est. cost this window: ${spend["total_cost"]:.2f} {delta_str}')
        lines.append(f'  Today so far: ${spend["today_cost"]:.2f}')
        for pr in spend["provider_rows"]:
            deg = f' · {pr["degraded"]} degraded' if pr["degraded"] else ''
            lines.append(f'    {pr["provider"]} · {pr["n"]} runs · ${pr["cost"]:.2f}{deg}')
        if spend["drift_n"] > 0:
            top = f' (mostly {spend["top_drift_model"]})' if spend["top_drift_model"] else ''
            lines.append(f'  WARNING: {spend["drift_n"]} runs fell off the pinned model{top}')
        else:
            lines.append('  All runs served on the pinned model.')
    lines.append("")

    work_section = sections["what_hermes_did"]
    lines += ["=" * 40, f'WHAT HERMES DID ({work_section["total"]})', "=" * 40, render_text_cards(work_section["rows"])]
    if work_section["shown"] < work_section["total"]:
        lines.append(f'  Showing {work_section["shown"]} of {work_section["total"]}.')
    queue_section = sections["queue"]
    lines += [
        "",
        "=" * 40,
        f'QUEUE ({queue_section["total"]})',
        "=" * 40,
        f'  {hermes_meta.get("ready",0)} agent-ready · snapshot {hermes_meta.get("generated","")}',
        render_text_cards(queue_section["rows"]),
    ]
    if queue_section["shown"] < queue_section["total"]:
        lines.append(f'  Showing {queue_section["shown"]} of {queue_section["total"]}.')
    lines += ["", "(Read-only status report. It does not fix anything - see ignite-babysit-hermes for that.)"]
    return "\n".join(lines)


def build_text(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min):
    model = build_report_view_model(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min)
    return build_text_view(model)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--window-min", type=int, default=360)
    p.add_argument("--header-file", help="JSON: when, health, model, auth, work_stoppage, needs_attention")
    p.add_argument("--out-html", default="/tmp/hermes_report.html")
    p.add_argument("--out-text", default="/tmp/hermes_report.txt")
    p.add_argument("--out-subject", default="/tmp/hermes_report_subject.txt")
    p.add_argument("--served-ledger", default=SERVED_LEDGER_DEFAULT)
    args = p.parse_args()

    header = {}
    if args.header_file and os.path.exists(args.header_file):
        try:
            header = json.load(open(args.header_file, encoding="utf-8"))
        except Exception as e:
            print(f"WARN: header-file unreadable ({e}); using empty header", file=sys.stderr)

    cache = {}
    hermes_rows, hermes_meta, snap = v1.build_hermes_list(cache)
    work_rows, work_counts = v1.build_work_list(args.window_min, cache)
    header = add_resolution_health(header, work_counts)

    snap_tasks = (snap or {}).get("tasks", [])
    snap_tasks_by_id = {t.get("id"): t for t in snap_tasks}

    window_rows, today_rows, previous_window_rows, ledger_err = load_served_ledger(
        args.served_ledger, args.window_min
    )
    if ledger_err:
        # A genuine read failure is NOT a $0.00 spend day — total_cost stays
        # None so every renderer (subject/headline/html/text/JSON summary) is
        # forced to go through _cost_display()/explicit None-handling instead
        # of silently formatting an unreadable ledger as "$0.00 spent."
        spend = {
            "empty": True,
            "error": ledger_err,
            "total_cost": None, "today_cost": None, "previous_window_cost": None, "cost_delta": None,
            "provider_rows": [], "providers_n": 0, "runs_n": 0, "drift_n": 0, "top_drift_model": None,
        }
        print(f"WARN: served ledger issue: {ledger_err}", file=sys.stderr)
        header = dict(header or {})
        existing = str(header.get("needs_attention") or "").strip()
        signal = f"served ledger unreadable ({ledger_err}) — spend figures unknown, not zero"
        header["needs_attention"] = f"{existing}; {signal}".strip("; ") if existing else signal
    elif not window_rows:
        spend = {
            "empty": True,
            "error": None,
            "total_cost": 0.0,
            "today_cost": sum(float(r.get("cost_usd") or 0) for r in today_rows),
            "previous_window_cost": sum(float(r.get("cost_usd") or 0) for r in previous_window_rows),
            "cost_delta": -sum(float(r.get("cost_usd") or 0) for r in previous_window_rows),
            "provider_rows": [], "providers_n": 0, "runs_n": 0, "drift_n": 0, "top_drift_model": None,
        }
    else:
        spend = summarize_spend(window_rows, today_rows, previous_window_rows)
        spend["empty"] = False
        spend["error"] = None

    scoreboard = build_scoreboard(hermes_rows, snap_tasks, hermes_meta, work_counts["completed"])

    # Deterministic verdict OVERWRITES whatever the header-file guessed for
    # work_stoppage — see "work-stoppage verdict" section above. The cron
    # skill's header JSON no longer needs to (and can no longer accidentally)
    # assert STALLED itself.
    header = dict(header or {})
    header["work_stoppage"] = compute_work_stoppage(
        hermes_meta.get("ready", 0), work_counts["completed"], scoreboard["in_progress"], work_counts,
    )

    alerts = build_alerts(hermes_rows, snap_tasks_by_id, header)
    model = build_report_view_model(
        header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, args.window_min,
    )
    subject = model["subject"]

    html_body = render_html_view(model)
    text_body = build_text_view(model)

    with open(args.out_html, "w", encoding="utf-8") as f:
        f.write(html_body)
    with open(args.out_text, "w", encoding="utf-8") as f:
        f.write(text_body)
    with open(args.out_subject, "w", encoding="utf-8") as f:
        f.write(subject)

    summary = {
        "out_html": args.out_html,
        "out_text": args.out_text,
        "out_subject": args.out_subject,
        "hermes_list_n": len(hermes_rows),
        "work_list_n": len(work_rows),
        "work_completed": work_counts["completed"],
        "work_completed_task_ids": work_counts.get("completed_task_ids", []),
        "briefs_scanned": work_counts["briefs_scanned"],
        "task_ids_found": work_counts["task_ids_found"],
        "tasks_resolved": work_counts.get("resolved", 0),
        "tasks_unresolved": work_counts.get("unresolved", 0),
        "unresolved_task_ids": work_counts.get("unresolved_task_ids", []),
        "snapshot_generated": hermes_meta.get("generated"),
        "suggested_subject": subject,
        "scoreboard_terms": {
            "ready": scoreboard["ready"],
            "in_progress": scoreboard["in_progress"],
            "current_in_review": scoreboard["in_review"],
            "blocked": scoreboard["blocked"],
            "validator_completed_window": scoreboard["validator_completed_window"],
        },
        "total_cost_usd": (round(spend["total_cost"], 4) if spend.get("total_cost") is not None else None),
        "today_so_far_cost_usd": (round(spend["today_cost"], 4) if spend.get("today_cost") is not None else None),
        "previous_window_cost_usd": (round(spend["previous_window_cost"], 4) if spend.get("previous_window_cost") is not None else None),
        "cost_delta_vs_previous_window_usd": (round(spend["cost_delta"], 4) if spend.get("cost_delta") is not None else None),
        "spend_ledger_error": spend.get("error"),
        **model["counts"],
        "providers_n": spend["providers_n"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
