#!/usr/bin/env python3
# COMPAT-ONLY, NO LIVE CALLER — RETIREMENT PENDING (q60 / 86e2gnz60).
# Vendored from live ~/.hermes/scripts/hermes_report_build_v2.py (sha256 b0b5f046…).
# The v2 card layout was folded into hermes_report_build.py; nothing imports this
# module. Kept in the canonical bundle only until a cross-surface caller check
# confirms zero callers, then a follow-up PR drops it. Do not build new deps on it.
"""
hermes_report_build_v2.py — CANDIDATE v2 of the Hermes status email (HTML + text).

Sign-off-gated candidate: does NOT touch v1 (hermes_report_build.py), the
postmark sender, the skill, or the cron. It is a peer script that reuses v1's
data-collection layer (queue snapshot + work-cron briefs + live clickup.mjs
resolution) by importing it as a module, and adds a new spend/model section
sourced from ~/.hermes/logs/writer-served.jsonl.

Why a rewrite of the rendering layer: v1's two 4-column HTML tables
(project | list | task | status) are unreadable in a mobile mail client —
that's the defect this candidate fixes. v2 restructures the SAME underlying
data into a mobile-first single-column layout:

  1. Headline banner (mirrors the subject)
  2. NEEDS YOU / ALERTS (stuck-in-flight, blocked/needs-human, health signals)
  3. SCOREBOARD (ready/in-progress/in-review/blocked/shipped, lanes)
  4. MODEL & SPEND (from the writer-served ledger: cost, per-provider, drift)
  5. ROSTER (work list + queue list, moved to the bottom, as stacked cards)

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
from urllib.parse import urlparse

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import hermes_report_build_v1lib as v1  # noqa: E402  (reuse v1's data-collection layer)

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    LOCAL_TZ = None

HERMES = os.path.expanduser("~/.hermes")
SERVED_LEDGER_DEFAULT = os.path.join(HERMES, "logs", "writer-served.jsonl")

STUCK_IN_PROGRESS_HOURS = 2.0


# ---------- spend / model ledger ----------

_CODEX_PROVIDER_ALIASES = {
    "codex",
    "codex-oauth",
    "openai-codex",
    "openai-codex-proxy",
}

_CODEX_PROXY_PORTS = {"8646", "8647"}


def _codex_proxy_base_url_is_proven(base_url):
    try:
        parsed = urlparse(base_url or "")
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost"} and str(port) in _CODEX_PROXY_PORTS


def _strip_jsonc_comments(text):
    out = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_jsonc_trailing_commas(text):
    out = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _configured_base_urls(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"baseURL", "base_url"} and isinstance(item, str):
                yield item.strip()
            yield from _configured_base_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from _configured_base_urls(item)


def _configured_codex_proxy_base_url():
    paths = [
        os.path.expanduser("~/.config/opencode/opencode.jsonc"),
        os.path.expanduser("~/.config/opencode/opencode.json"),
        os.path.expanduser("~/.config/opencode/config.jsonc"),
        os.path.expanduser("~/.config/opencode/config.json"),
    ]
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        lowered = text.lower()
        if "baseurl" not in lowered and "base_url" not in lowered:
            continue
        try:
            data = json.loads(_strip_jsonc_trailing_commas(_strip_jsonc_comments(text)))
        except Exception:
            continue
        for base_url in _configured_base_urls(data):
            if _codex_proxy_base_url_is_proven(base_url):
                return base_url
    return ""


def served_row_cost(row):
    """Return billable USD for a writer-served ledger row."""
    if not isinstance(row, dict):
        return 0.0
    raw = row.get("cost_usd")
    if raw is None:
        return 0.0
    try:
        cost = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if row.get("billing_mode"):
        return cost
    provider = (row.get("billing_provider") or row.get("served_provider") or "").strip().lower()
    base_url = (row.get("billing_base_url") or row.get("base_url") or row.get("baseURL") or "").strip()
    if provider in _CODEX_PROVIDER_ALIASES:
        if _codex_proxy_base_url_is_proven(base_url):
            return 0.0
        if not base_url and _configured_codex_proxy_base_url():
            return 0.0
    return cost

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
    """Parse writer-served.jsonl. Returns (window_rows, today_rows, yesterday_rows, error)."""
    if not path or not os.path.exists(path):
        return [], [], [], f"ledger not found: {path}"
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(minutes=window_min)
    rows = []
    try:
        with open(path, errors="replace") as f:
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

    today_local = _local_day(now)
    yesterday_local = today_local - datetime.timedelta(days=1) if today_local else None
    today_rows = [r for r in rows if _local_day(r["_dt"]) == today_local]
    yesterday_rows = [r for r in rows if _local_day(r["_dt"]) == yesterday_local]

    return window_rows, today_rows, yesterday_rows, None


def summarize_spend(window_rows, today_rows, yesterday_rows):
    total_cost = sum(served_row_cost(r) for r in window_rows)
    today_cost = sum(served_row_cost(r) for r in today_rows)
    yesterday_cost = sum(served_row_cost(r) for r in yesterday_rows)

    by_provider = collections.defaultdict(lambda: {"n": 0, "cost": 0.0, "degraded": 0})
    for r in window_rows:
        prov = r.get("served_provider") or r.get("served_model") or "unknown"
        by_provider[prov]["n"] += 1
        by_provider[prov]["cost"] += served_row_cost(r)
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
        "yesterday_cost": yesterday_cost,
        "cost_delta": today_cost - yesterday_cost,
        "provider_rows": provider_rows,
        "providers_n": len(provider_rows),
        "runs_n": len(window_rows),
        "drift_n": len(drift),
        "top_drift_model": top_drift_model,
    }


# ---------- alerts ----------

def _epoch_ms_to_dt(ms_str):
    try:
        return datetime.datetime.fromtimestamp(int(ms_str) / 1000.0, tz=datetime.timezone.utc)
    except Exception:
        return None


def build_alerts(hermes_rows, snap_tasks_by_id, header):
    """Deterministic stuck / blocked / health-signal alert cards."""
    alerts = []
    now = datetime.datetime.now(datetime.timezone.utc)

    for r in hermes_rows:
        tid = r.get("id")
        snap_t = snap_tasks_by_id.get(tid) or {}
        status = (r.get("status") or "").lower()
        tags = snap_t.get("tags") or []

        if "in progress" in status:
            du = snap_t.get("date_updated")
            dt = _epoch_ms_to_dt(du) if du else None
            if dt is not None:
                age_h = (now - dt).total_seconds() / 3600.0
                if age_h >= STUCK_IN_PROGRESS_HOURS:
                    alerts.append({
                        "kind": "stuck",
                        "name": r.get("name"),
                        "url": r.get("url"),
                        "detail": f"In progress {age_h:.0f}h, no update",
                        "sub": r.get("list"),
                    })

        if "blocked" in status or "needs human" in status or "needs-human" in tags:
            alerts.append({
                "kind": "blocked",
                "name": r.get("name"),
                "url": r.get("url"),
                "detail": "Awaiting you",
                "sub": r.get("list"),
            })

    work_stoppage = (header or {}).get("work_stoppage") or ""
    if work_stoppage and not work_stoppage.strip().endswith("-> OK") and "-> ok" not in work_stoppage.lower():
        alerts.append({
            "kind": "health",
            "name": "Work stoppage signal",
            "url": None,
            "detail": work_stoppage,
            "sub": "Health scan",
        })

    needs_attention = (header or {}).get("needs_attention") or ""
    for signal in needs_attention.split(";"):
        signal = signal.strip()
        if signal:
            alerts.append({
                "kind": "health",
                "name": "Needs attention",
                "url": None,
                "detail": signal,
                "sub": "Health scan",
            })

    return alerts


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
        "shipped": work_completed,
        "lane_code": lanes.get("code", 0),
        "lane_content": lanes.get("content", 0),
    }


# ---------- subject / headline ----------

def build_subject(scoreboard, spend, alerts_n):
    subj = (
        f"Hermes: {scoreboard['shipped']} shipped · {alerts_n} stuck · "
        f"${spend['total_cost']:.2f} · {scoreboard['ready']} ready"
    )
    return subj[:78]


def build_headline_emoji_text(scoreboard, spend, alerts_n):
    dot = "🟢" if alerts_n == 0 else "⚠️"
    return (
        f"{dot} {scoreboard['shipped']} shipped · "
        f"{'⚠️' if alerts_n else '✓'} {alerts_n} stuck · "
        f"💸 ${spend['total_cost']:.2f} · {scoreboard['ready']} ready"
    )


# ---------- HTML rendering ----------

def _esc(x):
    return html.escape(str(x)) if x is not None else ""


def render_html(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min, subject_line):
    h = header or {}
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
    banner_color = "#e8f5e9" if not alerts else "#fff3e0"
    border_color = "#2e7d32" if not alerts else "#e65100"
    headline = build_headline_emoji_text(scoreboard, spend, len(alerts))
    parts.append(
        f'<div style="background:{banner_color};border-bottom:3px solid {border_color};'
        f'padding:18px 20px;">'
        f'<div style="font-size:18px;font-weight:700;color:#1a1a1a;line-height:1.3">{_esc(headline)}</div>'
        f'<div style="font-size:13px;color:#666;margin-top:6px">'
        f'Hermes status · {when} · trailing {window_h_str}h</div>'
        '</div>'
    )

    parts.append('<div style="padding:20px">')

    # 2. Alerts
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">⚠️ Needs you</h2>')
    if not alerts:
        parts.append(
            '<div style="padding:14px 16px;background:#e8f5e9;border-left:4px solid #2e7d32;'
            'border-radius:4px;font-size:14px;color:#1a1a1a;margin-bottom:24px">'
            '✓ No blockers — nothing needs you right now.</div>'
        )
    else:
        parts.append('<div style="margin-bottom:24px">')
        for a in alerts:
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
            parts.append(
                '<div style="padding:12px 14px;margin-bottom:8px;background:#fff6f6;'
                'border-left:4px solid #b00020;border-radius:4px">'
                f'{title_html}'
                f'<div style="font-size:13px;color:#b00020;margin-top:4px;font-weight:600">{detail}</div>'
                + (f'<div style="font-size:12px;color:#888;margin-top:2px">{sub}</div>' if sub else '')
                + '</div>'
            )
        parts.append('</div>')

    # 3. Scoreboard
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">📊 Scoreboard</h2>')
    cells = [
        ("Ready", scoreboard["ready"], "#0b57d0"),
        ("In progress", scoreboard["in_progress"], "#b15c00"),
        ("In review", scoreboard["in_review"], "#0b69c7"),
        ("Blocked", scoreboard["blocked"], "#b00020"),
        ("Shipped", scoreboard["shipped"], "#0a7d33"),
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

    # 4. Model & spend
    parts.append('<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">💸 Model &amp; spend</h2>')
    if spend.get("empty"):
        parts.append(
            '<div style="padding:12px 14px;background:#f7f7f8;border-radius:4px;font-size:14px;'
            'color:#666;margin-bottom:24px">No served-by receipts in the window.</div>'
        )
    else:
        cost_line = f'Total est. cost this window: <b>${spend["total_cost"]:.2f}</b>'
        delta = spend["cost_delta"]
        if delta > 0.0001:
            cost_line += f' &nbsp; <span style="color:#b00020">▲ ${delta:.2f} vs yesterday</span>'
        elif delta < -0.0001:
            cost_line += f' &nbsp; <span style="color:#0a7d33">▼ ${abs(delta):.2f} vs yesterday</span>'
        else:
            cost_line += ' &nbsp; <span style="color:#888">(flat vs yesterday)</span>'
        parts.append(f'<div style="font-size:14px;color:#1a1a1a;margin-bottom:10px">{cost_line}</div>')

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

    # 5. Roster
    parts.append(
        f'<h2 style="margin:0 0 12px;font-size:16px;color:#1a1a1a">📋 What Hermes did '
        f'({len(work_rows)})</h2>'
    )
    parts.append(render_html_cards(work_rows))

    parts.append(
        f'<h2 style="margin:24px 0 12px;font-size:16px;color:#1a1a1a">📋 Queue '
        f'({len(hermes_rows)})</h2>'
    )
    parts.append(
        f'<div style="font-size:12px;color:#888;margin:-8px 0 12px">'
        f'{_esc(hermes_meta.get("ready"))} agent-ready · snapshot {_esc(hermes_meta.get("generated"))}</div>'
    )
    parts.append(render_html_cards(hermes_rows))

    parts.append(
        '<p style="margin:24px 0 0;font-size:11px;color:#999">'
        'Read-only status report (v2 candidate). It does not fix anything — '
        'see ignite-babysit-hermes for that.</p>'
    )

    parts.append('</div>')  # padding div
    parts.append('</div>')  # container
    parts.append('</body>')
    parts.append('</html>')
    return "\n".join(parts)


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


def build_text(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min):
    h = header or {}
    window_h = window_min / 60.0
    window_h_str = f"{window_h:.0f}" if window_h == int(window_h) else f"{window_h:.1f}"
    headline = build_headline_emoji_text(scoreboard, spend, len(alerts))

    lines = [
        headline,
        f'Hermes status · {h.get("when","")} · trailing {window_h_str}h',
        "",
        "=" * 40,
        "NEEDS YOU / ALERTS",
        "=" * 40,
    ]
    if not alerts:
        lines.append("  No blockers - nothing needs you right now.")
    else:
        for a in alerts:
            lines.append(f'  - {a.get("name")}: {a.get("detail")}')
            if a.get("sub"):
                lines.append(f'    ({a["sub"]})')
            if a.get("url"):
                lines.append(f'    {a["url"]}')
    lines.append("")

    lines += [
        "=" * 40,
        "SCOREBOARD",
        "=" * 40,
        f'  Ready: {scoreboard["ready"]}   In progress: {scoreboard["in_progress"]}   '
        f'In review: {scoreboard["in_review"]}   Blocked: {scoreboard["blocked"]}   '
        f'Shipped (window): {scoreboard["shipped"]}',
        f'  Lanes - Code: {scoreboard["lane_code"]} · Content: {scoreboard["lane_content"]}',
        "",
    ]

    lines += [
        "=" * 40,
        "MODEL & SPEND",
        "=" * 40,
    ]
    if spend.get("empty"):
        lines.append("  No served-by receipts in the window.")
    else:
        delta = spend["cost_delta"]
        if delta > 0.0001:
            delta_str = f'(up ${delta:.2f} vs yesterday)'
        elif delta < -0.0001:
            delta_str = f'(down ${abs(delta):.2f} vs yesterday)'
        else:
            delta_str = '(flat vs yesterday)'
        lines.append(f'  Total est. cost this window: ${spend["total_cost"]:.2f} {delta_str}')
        for pr in spend["provider_rows"]:
            deg = f' · {pr["degraded"]} degraded' if pr["degraded"] else ''
            lines.append(f'    {pr["provider"]} · {pr["n"]} runs · ${pr["cost"]:.2f}{deg}')
        if spend["drift_n"] > 0:
            top = f' (mostly {spend["top_drift_model"]})' if spend["top_drift_model"] else ''
            lines.append(f'  WARNING: {spend["drift_n"]} runs fell off the pinned model{top}')
        else:
            lines.append('  All runs served on the pinned model.')
    lines.append("")

    lines += [
        "=" * 40,
        f'WHAT HERMES DID ({len(work_rows)})',
        "=" * 40,
        render_text_cards(work_rows),
        "",
        "=" * 40,
        f'QUEUE ({len(hermes_rows)})',
        "=" * 40,
        f'  {hermes_meta.get("ready",0)} agent-ready · snapshot {hermes_meta.get("generated","")}',
        render_text_cards(hermes_rows),
        "",
        "(Read-only status report (v2 candidate). It does not fix anything - see ignite-babysit-hermes for that.)",
    ]
    return "\n".join(lines)


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
            header = json.load(open(args.header_file))
        except Exception as e:
            print(f"WARN: header-file unreadable ({e}); using empty header", file=sys.stderr)

    cache = {}
    hermes_rows, hermes_meta, snap = v1.build_hermes_list(cache)
    work_rows, work_counts = v1.build_work_list(args.window_min, cache)

    snap_tasks = (snap or {}).get("tasks", [])
    snap_tasks_by_id = {t.get("id"): t for t in snap_tasks}

    window_rows, today_rows, yesterday_rows, ledger_err = load_served_ledger(
        args.served_ledger, args.window_min
    )
    if ledger_err or not window_rows:
        spend = {
            "empty": True,
            "total_cost": 0.0, "today_cost": 0.0, "yesterday_cost": 0.0, "cost_delta": 0.0,
            "provider_rows": [], "providers_n": 0, "runs_n": 0, "drift_n": 0, "top_drift_model": None,
        }
        if ledger_err:
            print(f"WARN: served ledger issue: {ledger_err}", file=sys.stderr)
    else:
        spend = summarize_spend(window_rows, today_rows, yesterday_rows)
        spend["empty"] = False

    scoreboard = build_scoreboard(hermes_rows, snap_tasks, hermes_meta, work_counts["completed"])
    alerts = build_alerts(hermes_rows, snap_tasks_by_id, header)

    subject = build_subject(scoreboard, spend, len(alerts))

    html_body = render_html(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows,
                             args.window_min, subject)
    text_body = build_text(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows,
                            args.window_min)

    with open(args.out_html, "w") as f:
        f.write(html_body)
    with open(args.out_text, "w") as f:
        f.write(text_body)
    with open(args.out_subject, "w") as f:
        f.write(subject)

    summary = {
        "out_html": args.out_html,
        "out_text": args.out_text,
        "out_subject": args.out_subject,
        "hermes_list_n": len(hermes_rows),
        "work_list_n": len(work_rows),
        "work_completed": work_counts["completed"],
        "briefs_scanned": work_counts["briefs_scanned"],
        "task_ids_found": work_counts["task_ids_found"],
        "snapshot_generated": hermes_meta.get("generated"),
        "suggested_subject": subject,
        "total_cost_usd": round(spend["total_cost"], 4),
        "alerts_n": len(alerts),
        "providers_n": spend["providers_n"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
