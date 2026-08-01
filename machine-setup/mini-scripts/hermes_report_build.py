#!/usr/bin/env python3
"""
hermes_report_build.py — deterministic Hermes status email (HTML + text).

Human-first digest. Same data layer as before (queue snapshot + work-cron
briefs + ClickUp review queue + writer-served spend), but the email is built
for a 10-second scan, not a machine log dump.

Layout (empty sections are omitted):

  1. Verdict banner — one status + one sentence
  2. At a glance — completed / ready / in review / spend
  3. Needs you — blocked / needs-human only
  4. Worth watching — stale in-progress only
  5. System — non-OK health signals, humanized
  6. Review queue — count always; cards when non-empty / alert
  7. Activity — what Hermes did this window
  8. Spend — compact writer + daily totals (separate sources)
  9. Queue summary — one line (no full roster dump)

Data collection (_resolve_task, build_hermes_list, build_work_list) still
comes from `hermes_report_build_v1lib as v1`.

Outputs:
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
import re
import sys
import urllib.parse
import urllib.request
from urllib.parse import urlparse

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
DEFAULT_CLICKUP_TEAM_ID = "9017245888"
REVIEW_STATUSES = ("in review", "ready for review")
# Documented production default: alert once the workspace-wide review backlog
# reaches 25 tasks. Override only for deterministic tests or emergency tuning.
DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD = 25

STUCK_IN_PROGRESS_HOURS = 2.0
MAX_SECTION_ROWS = 10


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
    """Return billable USD for a writer-served ledger row.

    Post-fix rows carry routed cost plus ``billing_mode`` and are authoritative.
    Legacy Codex/OpenAI subscription rows carried raw OpenCode cost, so only
    zero them when the route is proven by row metadata or current OpenCode config.
    """
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
    total_cost = sum(served_row_cost(r) for r in window_rows)
    today_cost = sum(served_row_cost(r) for r in today_rows)
    previous_window_cost = sum(served_row_cost(r) for r in previous_window_rows)

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
        "writer_total_cost": total_cost,
        "today_cost": today_cost,
        "previous_window_cost": previous_window_cost,
        "cost_delta": total_cost - previous_window_cost,
        "provider_rows": provider_rows,
        "providers_n": len(provider_rows),
        "runs_n": len(window_rows),
        "drift_n": len(drift),
        "top_drift_model": top_drift_model,
    }


def load_guard_tracked_spend(today_str=None, spend_guard_module=None):
    """Return canonical spend-guard daily spend, distinct from writer receipts.

    spend_guard.py is the blocking cap's source of truth. Prefer its strict
    calculation so unreadable state.db/opencode data is reported as degraded
    instead of formatted as a false $0.00.
    """
    try:
        sg = spend_guard_module
        if sg is None:
            import spend_guard as sg  # noqa: F401
        if hasattr(sg, "_daily_spend_usd_strict"):
            total = sg._daily_spend_usd_strict(today_str)
        else:
            total = sg.daily_spend_usd(today_str)
        return {"guard_total_cost": float(total), "guard_error": None}
    except Exception as e:
        return {"guard_total_cost": None, "guard_error": str(e)}


def _cost_display(spend):
    """Render spend['total_cost'] for the subject/headline lines.

    A served-ledger READ FAILURE (spend['error'] set, total_cost=None) must
    never render as "$0.00" — that is indistinguishable from a real quiet
    period with zero spend and hides the failure from Colin. Only a genuine
    zero (ledger read fine, nothing served in the window) prints as $0.00.
    """
    if spend.get("error"):
        return "spend UNKNOWN (ledger unreadable)"
    return f"writer ${((spend.get('writer_total_cost', spend.get('total_cost')) or 0.0)):.2f}"


# ---------- workspace-wide review queue ----------

def _clickup_token():
    token = (os.environ.get("CLICKUP_API_TOKEN") or "").strip()
    if token:
        return token
    try:
        from agent import lazy_secret_resolver
        token = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        token = None
    return (token or "").strip()


def _clickup_get_json(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": token or _clickup_token()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _task_row_from_clickup(t):
    folder = t.get("folder") or t.get("project") or {}
    lst = t.get("list") or {}
    st = t.get("status") or {}
    status = st.get("status") if isinstance(st, dict) else (st or "")
    return {
        "id": t.get("id"),
        "project": (folder.get("name") or "—") if isinstance(folder, dict) else "—",
        "list": (lst.get("name") or "—") if isinstance(lst, dict) else "—",
        "list_id": (lst.get("id") or "") if isinstance(lst, dict) else "",
        "name": t.get("name") or t.get("id"),
        "url": t.get("url") or f'https://app.clickup.com/t/{t.get("id")}',
        "status": status or "unknown",
        "status_type": st.get("type", "") if isinstance(st, dict) else "",
        "date_closed": t.get("date_closed"),
        "date_done": t.get("date_done"),
        "date_updated": t.get("date_updated"),
        "parent": t.get("parent"),
        "resolution_error": None,
    }


def fetch_workspace_review_queue(team_id=DEFAULT_CLICKUP_TEAM_ID, token=None, get_json=None, max_pages=100):
    """Paginated, deduplicated team-wide ClickUp query for all review tasks.

    This deliberately does not discover boards/lists first: status filtering at
    the team/task endpoint sees unmapped-board tasks and subtasks across the
    whole workspace. On failure, returns a degraded metadata error instead of a
    false zero.
    """
    get_json = get_json or _clickup_get_json
    token = token if token is not None else _clickup_token()
    if not token:
        return [], {"error": "CLICKUP_API_TOKEN unavailable", "statuses": list(REVIEW_STATUSES)}

    rows_by_id = {}
    pages = 0
    try:
        for status in REVIEW_STATUSES:
            page = 0
            while True:
                params = [("page", str(page)), ("subtasks", "true"), ("include_closed", "false"), ("statuses[]", status)]
                q = urllib.parse.urlencode(params)
                data = get_json(f"https://api.clickup.com/api/v2/team/{team_id}/task?{q}", token)
                pages += 1
                tasks = data.get("tasks") or []
                for task in tasks:
                    row = _task_row_from_clickup(task)
                    if row.get("id") and (row.get("status") or "").lower() in REVIEW_STATUSES:
                        rows_by_id.setdefault(row["id"], row)
                if data.get("last_page", True) or not tasks:
                    break
                page += 1
                if page >= max_pages:
                    raise RuntimeError(f"ClickUp pagination exceeded {max_pages} pages for status {status!r}")
    except Exception as e:
        return [], {"error": f"ClickUp review queue degraded: {e}", "statuses": list(REVIEW_STATUSES), "pages": pages}

    rows = sorted(rows_by_id.values(), key=lambda r: ((r.get("status") or ""), (r.get("list") or ""), (r.get("name") or "")))
    return rows, {"error": None, "statuses": list(REVIEW_STATUSES), "pages": pages, "deduped": len(rows)}


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

def build_scoreboard(hermes_rows, snap_tasks, hermes_meta, work_completed, review_rows=None, review_error=False):
    statuses = collections.Counter((r.get("status") or "").lower() for r in hermes_rows)
    in_progress = statuses.get("in progress", 0)
    if review_error:
        in_review = "UNKNOWN"
    else:
        in_review = len(review_rows) if review_rows is not None else statuses.get("in review", 0) + statuses.get("ready for review", 0)
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


# ---------- subject / verdict / copy ----------

def summarize_alerts(alerts):
    counts = collections.Counter(a.get("kind") for a in alerts)
    return {
        "watch_list": counts.get("stuck", 0),
        "action_required": counts.get("blocked", 0),
        "system_signals": counts.get("health", 0),
        "review_backlog": counts.get("review_backlog", 0),
        "alerts_n": len(alerts),
    }


def build_review_backlog_alert(review_rows, review_meta, threshold):
    if review_meta.get("error"):
        return {
            "kind": "health",
            "name": "Review queue unavailable",
            "url": None,
            "detail": (
                "Couldn't load the workspace review queue — count is unknown, not zero. "
                f"({review_meta['error']})"
            ),
            "sub": "ClickUp",
        }
    if len(review_rows) >= threshold:
        return {
            "kind": "review_backlog",
            "name": "Review backlog is high",
            "url": None,
            "detail": (
                f"{len(review_rows)} tasks waiting in review "
                f"(alert threshold is {threshold})"
            ),
            "sub": "ClickUp",
        }
    return None


def _parse_work_stoppage_fields(raw):
    """Extract ready/completed/in_progress/live_claims and verdict from machine string."""
    text = (raw or "").strip()
    fields = {}
    for key in ("ready", "completed", "in_progress", "live_claims"):
        m = re.search(rf"{key}=([0-9?]+)", text)
        if m:
            fields[key] = m.group(1)
    verdict = "OK"
    if "-> STALLED" in text:
        verdict = "STALLED"
    elif "-> UNKNOWN" in text:
        verdict = "UNKNOWN"
    elif "-> OK" in text or text.lower().endswith("-> ok"):
        verdict = "OK"
    return fields, verdict, text


def humanize_work_stoppage(raw):
    """Turn the machine work-stoppage string into a sentence a human can act on."""
    fields, verdict, text = _parse_work_stoppage_fields(raw)
    if not text:
        return ""
    ready = fields.get("ready", "?")
    completed = fields.get("completed", "?")
    in_progress = fields.get("in_progress", "?")
    live_claims = fields.get("live_claims", "?")
    if verdict == "OK":
        return "Work is flowing."
    if verdict == "STALLED":
        return (
            f"Ready work isn't moving — {ready} ready, {completed} completed this window, "
            f"{in_progress} in progress, {live_claims} live claims."
        )
    if "live claim count unavailable" in text:
        return (
            "Can't confirm whether work is moving — the claim store couldn't be read. "
            f"(ready={ready}, completed={completed}, in progress={in_progress})"
        )
    if "work evidence unresolved" in text:
        return (
            f"{in_progress} tasks show in progress, but work evidence for this window "
            "couldn't be verified."
        )
    return "Work-stoppage check was inconclusive."


def humanize_health_alert(alert):
    """Return a display copy of a health alert with humanized detail when applicable."""
    out = dict(alert or {})
    name = (out.get("name") or "").lower()
    detail = out.get("detail") or ""
    if "work stoppage" in name or "-> STALLED" in detail or "-> UNKNOWN" in detail:
        out["name"] = "Work may be stalled" if "STALLED" in detail else "Work status unclear"
        out["detail"] = humanize_work_stoppage(detail)
        out["sub"] = out.get("sub") or "Health"
    elif "served ledger" in detail.lower() or "ledger unreadable" in detail.lower():
        out["name"] = out.get("name") if out.get("name") != "Needs attention" else "Spend ledger unreadable"
        if "Needs attention" in (out.get("name") or "") or out.get("name") == "Spend ledger unreadable":
            out["name"] = "Spend ledger unreadable"
        out["detail"] = "Writer spend for this window is unknown — the receipt ledger couldn't be read."
    elif "work-card resolution degraded" in detail.lower() or "resolution degraded" in detail.lower():
        out["name"] = "Some activity cards couldn't be resolved"
        out["detail"] = detail.replace("ClickUp work-card resolution degraded: ", "Resolved ")
    return out


def _window_label(window_min):
    hours = window_min / 60.0
    if hours == int(hours):
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


def _completed_count(scoreboard):
    return scoreboard.get("validator_completed_window", scoreboard.get("shipped", 0))


def _spend_short(spend):
    """Compact spend for subject / glance — never formats ledger failure as $0."""
    if spend.get("error"):
        return "spend unknown"
    cost = spend.get("writer_total_cost", spend.get("total_cost"))
    if cost is None:
        return "spend unknown"
    return f"${float(cost):.2f}"


def build_verdict(scoreboard, spend, alert_summary, header=None):
    """Single human verdict driving subject + banner. Priority: stalled > backlog > needs you > signals > watch > ok."""
    header = header or {}
    raw_stop = (header.get("work_stoppage") or "")
    _, stop_verdict, _ = _parse_work_stoppage_fields(raw_stop)
    completed = _completed_count(scoreboard)
    spend_bit = _spend_short(spend)
    action_n = alert_summary.get("action_required", 0)
    watch_n = alert_summary.get("watch_list", 0)
    signal_n = alert_summary.get("system_signals", 0)
    review_n = alert_summary.get("review_backlog", 0)
    in_review = scoreboard.get("in_review", 0)

    if stop_verdict == "STALLED":
        return {
            "level": "stalled",
            "label": "Stalled",
            "summary": humanize_work_stoppage(raw_stop),
            "subject": "Hermes: stalled — ready work isn't moving",
        }
    if review_n:
        count = in_review if in_review not in (None, "UNKNOWN") else "many"
        return {
            "level": "alert",
            "label": "Review backlog",
            "summary": f"{count} tasks are waiting on review (over the alert threshold).",
            "subject": f"Hermes: review backlog — {count} waiting",
        }
    if action_n:
        noun = "task" if action_n == 1 else "tasks"
        return {
            "level": "attention",
            "label": "Needs you",
            "summary": f"{action_n} blocked {noun} waiting on a human decision.",
            "subject": f"Hermes: needs you — {action_n} blocked",
        }
    if stop_verdict == "UNKNOWN" or signal_n:
        return {
            "level": "attention",
            "label": "Check signals",
            "summary": (
                humanize_work_stoppage(raw_stop)
                if stop_verdict == "UNKNOWN"
                else f"{signal_n} system signal{'s' if signal_n != 1 else ''} need a look."
            ),
            "subject": "Hermes: check signals",
        }
    if watch_n:
        noun = "task" if watch_n == 1 else "tasks"
        return {
            "level": "watch",
            "label": "Worth watching",
            "summary": f"{watch_n} in-progress {noun} went quiet for 2h+.",
            "subject": f"Hermes: watching — {watch_n} quiet {noun}",
        }
    return {
        "level": "ok",
        "label": "All clear",
        "summary": f"{completed} completed this window · {spend_bit} writer spend · {scoreboard.get('ready', 0)} ready.",
        "subject": f"Hermes: all clear — {completed} completed · {spend_bit}",
    }


def build_subject(scoreboard, spend, alert_summary, header=None):
    return build_verdict(scoreboard, spend, alert_summary, header)["subject"]


def build_headline_emoji_text(scoreboard, spend, alert_summary, header=None):
    """Back-compat name: returns the human headline sentence (no emoji soup)."""
    v = build_verdict(scoreboard, spend, alert_summary, header)
    return f"{v['label']} — {v['summary']}"


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


def build_report_view_model(
    header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min,
    review_rows=None, review_meta=None, review_threshold=DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD,
):
    """Single structured report model consumed by subject, body, and JSON summary."""
    h = header or {}
    review_rows = list(review_rows or [])
    review_meta = review_meta or {"error": None}
    action_required = [a for a in alerts if a.get("kind") == "blocked"]
    watch_list = [a for a in alerts if a.get("kind") == "stuck"]
    system_signals = [humanize_health_alert(a) for a in alerts if a.get("kind") == "health"]
    review_backlog = [a for a in alerts if a.get("kind") == "review_backlog"]
    sections = {
        "action_required": {"title": "Needs you", "items": action_required},
        "watch_list": {"title": "Worth watching", "items": watch_list},
        "system_signals": {"title": "System", "items": system_signals},
        "workspace_review_queue": {"title": "Review queue", **_bounded(review_rows)},
        "what_hermes_did": {"title": "Activity", **_bounded(work_rows)},
        "queue": {"title": "Queue", **_bounded(hermes_rows)},
    }
    alert_summary = {
        "action_required": len(action_required),
        "watch_list": len(watch_list),
        "system_signals": len(system_signals),
        "review_backlog": len(review_backlog),
        "alerts_n": len(action_required) + len(watch_list) + len(system_signals) + len(review_backlog),
    }
    verdict = build_verdict(scoreboard, spend, alert_summary, h)
    return {
        "header": h,
        "scoreboard": scoreboard,
        "spend": spend,
        "sections": sections,
        "counts": alert_summary,
        "hermes_meta": hermes_meta or {},
        "review_meta": review_meta,
        "review_threshold": review_threshold,
        "window_min": window_min,
        "verdict": verdict,
        "subject": verdict["subject"],
        "headline": f"{verdict['label']} — {verdict['summary']}",
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

_VERDICT_STYLES = {
    "ok": {"bg": "#eef7f1", "accent": "#0f6b3c", "label": "#0f6b3c"},
    "watch": {"bg": "#fff8eb", "accent": "#9a6700", "label": "#9a6700"},
    "attention": {"bg": "#fff4ed", "accent": "#c2410c", "label": "#c2410c"},
    "stalled": {"bg": "#fef2f2", "accent": "#b91c1c", "label": "#b91c1c"},
    "alert": {"bg": "#fef2f2", "accent": "#b91c1c", "label": "#b91c1c"},
}


def _esc(x):
    return html.escape(str(x)) if x is not None else ""


def _section_heading(title):
    return (
        f'<h2 style="margin:28px 0 10px;font-size:13px;font-weight:700;letter-spacing:0.04em;'
        f'text-transform:uppercase;color:#6b7280">{_esc(title)}</h2>'
    )


def _metric_cell(label, value, emphasize=False):
    color = "#111827" if emphasize else "#374151"
    return (
        '<td style="width:25%;padding:14px 8px;text-align:center;vertical-align:top">'
        f'<div style="font-size:22px;font-weight:700;color:{color};line-height:1.1">{_esc(value)}</div>'
        f'<div style="font-size:11px;color:#6b7280;margin-top:4px;text-transform:uppercase;'
        f'letter-spacing:0.03em">{_esc(label)}</div>'
        '</td>'
    )


def render_html_view(model):
    h = model["header"]
    scoreboard = model["scoreboard"]
    spend = model["spend"]
    sections = model["sections"]
    hermes_meta = model["hermes_meta"]
    review_meta = model.get("review_meta") or {"error": None}
    review_threshold = model.get("review_threshold", DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD)
    window_min = model["window_min"]
    subject_line = model["subject"]
    verdict = model.get("verdict") or build_verdict(scoreboard, spend, model["counts"], h)
    when = _esc(h.get("when", ""))
    window_label = _window_label(window_min)
    style = _VERDICT_STYLES.get(verdict.get("level"), _VERDICT_STYLES["attention"])

    css_body = (
        "margin:0;padding:0;background:#eceff3;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        "color:#111827;"
    )
    css_container = "max-width:560px;margin:0 auto;background:#ffffff;"

    parts = [
        '<!DOCTYPE html>',
        '<html><head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<title>{_esc(subject_line)}</title>',
        '</head>',
        f'<body style="{css_body}">',
        f'<div style="{css_container}">',
    ]

    # 1. Verdict banner
    parts.append(
        f'<div style="background:{style["bg"]};border-bottom:3px solid {style["accent"]};padding:22px 24px;">'
        f'<div style="font-size:12px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;'
        f'color:{style["label"]};margin-bottom:6px">Hermes · {_esc(verdict["label"])}</div>'
        f'<div style="font-size:20px;font-weight:700;color:#111827;line-height:1.35">'
        f'{_esc(verdict["summary"])}</div>'
        f'<div style="font-size:13px;color:#6b7280;margin-top:10px">'
        f'{when} · last {window_label}</div>'
        '</div>'
    )

    parts.append('<div style="padding:8px 24px 28px">')

    # 2. At a glance
    completed = _completed_count(scoreboard)
    review_glance = "—" if review_meta.get("error") else scoreboard["in_review"]
    spend_glance = _spend_short(spend)
    parts.append(_section_heading("At a glance"))
    parts.append(
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;background:#f9fafb;border-radius:8px;margin-bottom:4px">'
        '<tr>'
        f'{_metric_cell("Completed", completed, emphasize=True)}'
        f'{_metric_cell("Ready", scoreboard["ready"])}'
        f'{_metric_cell("In review", review_glance)}'
        f'{_metric_cell("Writer spend", spend_glance)}'
        '</tr></table>'
    )
    extra_bits = [
        f'{scoreboard["in_progress"]} in progress',
        f'{scoreboard["blocked"]} blocked',
    ]
    if scoreboard.get("lane_code") or scoreboard.get("lane_content"):
        extra_bits.append(
            f'lanes {scoreboard["lane_code"]} code / {scoreboard["lane_content"]} content'
        )
    parts.append(
        f'<div style="font-size:12px;color:#6b7280;margin:8px 0 0">'
        f'{" · ".join(extra_bits)}</div>'
    )

    # 3. Needs you (omit when empty)
    action_items = sections["action_required"]["items"]
    if action_items:
        parts.append(_section_heading(f'Needs you ({len(action_items)})'))
        for a in action_items:
            parts.append(render_html_alert_card(a, "#b91c1c"))

    # 4. Worth watching
    watch_items = sections["watch_list"]["items"]
    if watch_items:
        parts.append(_section_heading(f'Worth watching ({len(watch_items)})'))
        for a in watch_items:
            parts.append(render_html_alert_card(a, "#9a6700"))

    # 5. System
    system_items = sections["system_signals"]["items"]
    if system_items:
        parts.append(_section_heading(f'System ({len(system_items)})'))
        for a in system_items:
            parts.append(render_html_alert_card(a, "#4b5563"))

    # 6. Review queue
    review_section = sections["workspace_review_queue"]
    review_rows = review_section["rows"]
    review_count_label = "unknown" if review_meta.get("error") else str(review_section["total"])
    parts.append(_section_heading(f'Review queue ({review_count_label})'))
    if review_meta.get("error"):
        parts.append(
            '<div style="padding:12px 14px;background:#fef2f2;border-left:4px solid #b91c1c;'
            'border-radius:4px;font-size:14px;color:#991b1b;margin-bottom:12px">'
            f'Couldn\'t load the review queue — count is unknown, not zero. '
            f'({_esc(review_meta.get("error"))})</div>'
        )
    elif review_section["total"] >= review_threshold:
        parts.append(
            '<div style="padding:14px 16px;background:#fef2f2;border-left:4px solid #b91c1c;'
            'border-radius:4px;font-size:15px;color:#991b1b;margin-bottom:12px;font-weight:700">'
            f'REVIEW BACKLOG ALERT: {review_section["total"]} tasks waiting '
            f'(threshold {review_threshold}).</div>'
        )
    if review_rows:
        parts.append(render_html_cards(review_rows))
        if review_section["shown"] < review_section["total"]:
            parts.append(
                f'<div style="font-size:12px;color:#9ca3af;margin:-4px 0 8px">'
                f'Showing {review_section["shown"]} of {review_section["total"]}.</div>'
            )
    elif not review_meta.get("error"):
        parts.append(
            '<div style="font-size:14px;color:#6b7280;margin-bottom:8px">Nothing waiting on review.</div>'
        )

    # 7. Activity
    work_section = sections["what_hermes_did"]
    parts.append(_section_heading(f'Activity ({work_section["total"]})'))
    if work_section["rows"]:
        parts.append(render_html_cards(work_section["rows"]))
        if work_section["shown"] < work_section["total"]:
            parts.append(
                f'<div style="font-size:12px;color:#9ca3af;margin:-4px 0 8px">'
                f'Showing {work_section["shown"]} of {work_section["total"]}.</div>'
            )
    else:
        parts.append(
            '<div style="font-size:14px;color:#6b7280;margin-bottom:8px">'
            'No completed work cards in this window.</div>'
        )

    # 8. Spend (compact)
    parts.append(_section_heading("Spend"))
    parts.append(render_html_spend(spend, window_label))

    # 9. Queue summary (no roster dump)
    queue_section = sections["queue"]
    parts.append(_section_heading("Queue"))
    parts.append(
        f'<div style="font-size:14px;color:#374151;line-height:1.5">'
        f'{queue_section["total"]} tasks on the Hermes board · '
        f'{_esc(hermes_meta.get("ready", 0))} ready'
        + (
            f'<br><span style="font-size:12px;color:#9ca3af">Snapshot {_esc(hermes_meta.get("generated"))}</span>'
            if hermes_meta.get("generated") else ""
        )
        + '</div>'
    )

    parts.append(
        '<p style="margin:28px 0 0;font-size:11px;color:#9ca3af;line-height:1.4">'
        'Read-only status digest. It does not fix anything.</p>'
    )
    parts.append('</div></div></body></html>')
    return "\n".join(parts)


def render_html_spend(spend, window_label):
    chunks = []
    if spend.get("guard_error"):
        chunks.append(
            '<div style="padding:12px 14px;background:#fef2f2;border-left:4px solid #b91c1c;'
            'border-radius:4px;font-size:14px;color:#991b1b;margin-bottom:10px">'
            f'Daily spend unknown ({_esc(str(spend["guard_error"]))}) — not reported as $0.00.</div>'
        )
    else:
        guard = spend.get("guard_total_cost")
        guard_txt = f"${float(guard):.2f}" if guard is not None else "unknown"
        chunks.append(
            f'<div style="font-size:14px;color:#111827;margin-bottom:8px">'
            f'Daily total (all sources): <b>{guard_txt}</b></div>'
        )

    if spend.get("error"):
        chunks.append(
            '<div style="padding:12px 14px;background:#fef2f2;border-left:4px solid #b91c1c;'
            'border-radius:4px;font-size:14px;color:#991b1b;margin-bottom:8px">'
            f'Writer spend for this {window_label} is unknown — ledger unreadable '
            f'({_esc(str(spend["error"]))}).</div>'
        )
    elif spend.get("empty"):
        chunks.append(
            f'<div style="font-size:14px;color:#6b7280;margin-bottom:8px">'
            f'No writer spend in the last {window_label}.</div>'
        )
    else:
        delta = spend.get("cost_delta") or 0
        if delta > 0.0001:
            delta_html = f'<span style="color:#b91c1c">↑ ${delta:.2f} vs prior {window_label}</span>'
        elif delta < -0.0001:
            delta_html = f'<span style="color:#0f6b3c">↓ ${abs(delta):.2f} vs prior {window_label}</span>'
        else:
            delta_html = f'<span style="color:#9ca3af">flat vs prior {window_label}</span>'
        chunks.append(
            f'<div style="font-size:14px;color:#111827;margin-bottom:6px">'
            f'Writer ({window_label}): <b>${spend["writer_total_cost"]:.2f}</b> · {delta_html}</div>'
        )
        if spend.get("today_cost") is not None:
            chunks.append(
                f'<div style="font-size:13px;color:#6b7280;margin-bottom:8px">'
                f'Today so far (writer): ${spend["today_cost"]:.2f}</div>'
            )
        if spend.get("provider_rows"):
            for pr in spend["provider_rows"]:
                deg = f' · {pr["degraded"]} degraded' if pr.get("degraded") else ""
                run_label = "run" if pr["n"] == 1 else "runs"
                chunks.append(
                    '<div style="padding:8px 12px;background:#f9fafb;border-radius:4px;margin-bottom:4px;'
                    'font-size:13px;color:#374151">'
                    f'{_esc(pr["provider"])} · {pr["n"]} {run_label} · ${pr["cost"]:.2f}{deg}'
                    '</div>'
                )
        if spend.get("drift_n", 0) > 0:
            top = f' (mostly {_esc(spend["top_drift_model"])})' if spend.get("top_drift_model") else ""
            drift_n = spend["drift_n"]
            run_label = "run" if drift_n == 1 else "runs"
            chunks.append(
                f'<div style="font-size:13px;color:#9a6700;margin-top:8px">'
                f'{drift_n} {run_label} used a different model than pinned{top}.</div>'
            )

    chunks.append(
        '<div style="font-size:11px;color:#9ca3af;margin-top:10px">'
        'Writer and daily totals are separate sources — they are not added together.</div>'
    )
    return "\n".join(chunks)


def render_html_alert_card(a, color):
    name = _esc(a.get("name") or "")
    url = a.get("url")
    detail = _esc(a.get("detail") or "")
    sub = _esc(a.get("sub") or "")
    if url:
        title_html = (
            f'<a href="{_esc(url)}" style="color:#1d4ed8;text-decoration:none;'
            f'font-weight:600;font-size:15px">{name}</a>'
        )
    else:
        title_html = f'<span style="font-weight:600;font-size:15px;color:#111827">{name}</span>'
    return (
        '<div style="padding:12px 14px;margin-bottom:8px;background:#fafafa;'
        f'border-left:4px solid {color};border-radius:4px">'
        f'{title_html}'
        f'<div style="font-size:13px;color:#374151;margin-top:4px;line-height:1.4">{detail}</div>'
        + (f'<div style="font-size:12px;color:#9ca3af;margin-top:4px">{sub}</div>' if sub else "")
        + '</div>'
    )


def render_html(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, *args, subject_line=None):
    if len(args) == 1:
        review_rows, review_meta, review_threshold, window_min = [], {"error": None}, DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD, args[0]
    elif len(args) == 2:
        review_rows, review_meta, review_threshold, window_min = [], {"error": None}, DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD, args[0]
        subject_line = subject_line or args[1]
    elif len(args) == 4:
        review_rows, review_meta, review_threshold, window_min = args
    elif len(args) == 5:
        review_rows, review_meta, review_threshold, window_min = args[:4]
        subject_line = subject_line or args[4]
    else:
        raise TypeError("render_html expects window_min or review_rows, review_meta, review_threshold, window_min[, subject_line]")
    model = build_report_view_model(
        header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min,
        review_rows, review_meta, review_threshold,
    )
    if subject_line:
        model["subject"] = subject_line
    return render_html_view(model)


def render_html_cards(rows):
    if not rows:
        return ""
    out = ['<div style="margin-bottom:8px">']
    for r in rows:
        name = _esc(r.get("name"))
        url = _esc(r.get("url") or "")
        status = r.get("status") or "unknown"
        color = v1._status_color(status)
        project = _esc(r.get("project", "—"))
        lst = _esc(r.get("list", "—"))
        out.append(
            '<div style="padding:10px 12px;margin-bottom:6px;background:#f9fafb;'
            'border:1px solid #eef0f3;border-radius:6px">'
            f'<div style="font-size:11px;font-weight:600;color:{color};text-transform:uppercase;'
            f'letter-spacing:0.03em;margin-bottom:4px">{_esc(status)}</div>'
            f'<a href="{url}" style="color:#1d4ed8;text-decoration:none;font-size:14px;'
            f'font-weight:600;line-height:1.3">{name}</a>'
            f'<div style="font-size:12px;color:#9ca3af;margin-top:3px">{project} · {lst}</div>'
            '</div>'
        )
    out.append('</div>')
    return "\n".join(out)


# ---------- text rendering ----------

def render_text_cards(rows):
    if not rows:
        return "  (none)"
    lines = []
    for r in rows:
        lines.append(f'  · {r.get("name")}  [{r.get("status", "unknown")}]')
        lines.append(f'    {r.get("project", "—")} · {r.get("list", "—")}')
        if r.get("url"):
            lines.append(f'    {r["url"]}')
    return "\n".join(lines)


def render_text_alerts(alerts):
    lines = []
    for a in alerts:
        lines.append(f'  · {a.get("name")}')
        if a.get("detail"):
            lines.append(f'    {a["detail"]}')
        if a.get("sub"):
            lines.append(f'    ({a["sub"]})')
        if a.get("url"):
            lines.append(f'    {a["url"]}')
    return lines


def _text_spend_block(spend, window_label):
    lines = []
    if spend.get("error"):
        lines.append(
            f"  Writer ({window_label}): unknown — ledger unreadable ({spend['error']})"
        )
    elif spend.get("empty"):
        lines.append(f"  Writer ({window_label}): no spend")
    else:
        delta = spend.get("cost_delta") or 0
        if delta > 0.0001:
            delta_str = f"(up ${delta:.2f} vs prior {window_label})"
        elif delta < -0.0001:
            delta_str = f"(down ${abs(delta):.2f} vs prior {window_label})"
        else:
            delta_str = f"(flat vs prior {window_label})"
        lines.append(f'  Writer ({window_label}): ${spend["writer_total_cost"]:.2f} {delta_str}')
        if spend.get("today_cost") is not None:
            lines.append(f'  Today so far (writer): ${spend["today_cost"]:.2f}')
        for pr in spend.get("provider_rows") or []:
            deg = f' · {pr["degraded"]} degraded' if pr.get("degraded") else ""
            run_label = "run" if pr["n"] == 1 else "runs"
            lines.append(f'    {pr["provider"]} · {pr["n"]} {run_label} · ${pr["cost"]:.2f}{deg}')
        if spend.get("drift_n", 0) > 0:
            top = f' (mostly {spend["top_drift_model"]})' if spend.get("top_drift_model") else ""
            drift_n = spend["drift_n"]
            run_label = "run" if drift_n == 1 else "runs"
            lines.append(f'  {drift_n} {run_label} used a different model than pinned{top}')
    if spend.get("guard_error"):
        lines.append(f'  Daily total (all sources): unknown ({spend["guard_error"]}) — not $0.00')
    else:
        guard = spend.get("guard_total_cost")
        guard_txt = f"${float(guard):.2f}" if guard is not None else "unknown"
        lines.append(f'  Daily total (all sources): {guard_txt}')
    lines.append("  Writer and daily totals are separate sources — they are not added together.")
    return lines


def build_text_view(model):
    h = model["header"]
    scoreboard = model["scoreboard"]
    spend = model["spend"]
    sections = model["sections"]
    hermes_meta = model["hermes_meta"]
    review_meta = model.get("review_meta") or {"error": None}
    review_threshold = model.get("review_threshold", DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD)
    window_label = _window_label(model["window_min"])
    verdict = model.get("verdict") or build_verdict(scoreboard, spend, model["counts"], h)

    lines = [
        f"HERMES · {verdict['label'].upper()}",
        verdict["summary"],
        f'{h.get("when", "")} · last {window_label}'.strip(" ·"),
        "",
        "AT A GLANCE",
        f'  Completed {_completed_count(scoreboard)}  ·  Ready {scoreboard["ready"]}  ·  '
        f'In review {"unknown" if review_meta.get("error") else scoreboard["in_review"]}  ·  '
        f'Writer {_spend_short(spend)}',
        f'  {scoreboard["in_progress"]} in progress · {scoreboard["blocked"]} blocked'
        + (
            f' · lanes {scoreboard["lane_code"]} code / {scoreboard["lane_content"]} content'
            if scoreboard.get("lane_code") or scoreboard.get("lane_content") else ""
        ),
    ]

    action_items = sections["action_required"]["items"]
    if action_items:
        lines += ["", f"NEEDS YOU ({len(action_items)})"]
        lines.extend(render_text_alerts(action_items))

    watch_items = sections["watch_list"]["items"]
    if watch_items:
        lines += ["", f"WORTH WATCHING ({len(watch_items)})"]
        lines.extend(render_text_alerts(watch_items))

    system_items = sections["system_signals"]["items"]
    if system_items:
        lines += ["", f"SYSTEM ({len(system_items)})"]
        lines.extend(render_text_alerts(system_items))

    review_section = sections["workspace_review_queue"]
    review_count = "unknown" if review_meta.get("error") else review_section["total"]
    lines += ["", f"REVIEW QUEUE ({review_count})"]
    if review_meta.get("error"):
        lines.append(f'  Couldn\'t load review queue — count unknown, not zero. ({review_meta["error"]})')
    elif review_section["total"] >= review_threshold:
        lines.append(
            f'  REVIEW BACKLOG ALERT: {review_section["total"]} tasks waiting '
            f'(threshold {review_threshold}).'
        )
    if review_section["rows"]:
        lines.append(render_text_cards(review_section["rows"]))
        if review_section["shown"] < review_section["total"]:
            lines.append(f'  Showing {review_section["shown"]} of {review_section["total"]}.')
    elif not review_meta.get("error"):
        lines.append("  Nothing waiting on review.")

    work_section = sections["what_hermes_did"]
    lines += ["", f"ACTIVITY ({work_section['total']})"]
    if work_section["rows"]:
        lines.append(render_text_cards(work_section["rows"]))
        if work_section["shown"] < work_section["total"]:
            lines.append(f'  Showing {work_section["shown"]} of {work_section["total"]}.')
    else:
        lines.append("  No completed work cards in this window.")

    lines += ["", "SPEND"]
    lines.extend(_text_spend_block(spend, window_label))

    queue_section = sections["queue"]
    lines += [
        "",
        "QUEUE",
        f'  {queue_section["total"]} tasks on the Hermes board · {hermes_meta.get("ready", 0)} ready',
    ]
    if hermes_meta.get("generated"):
        lines.append(f'  Snapshot {hermes_meta["generated"]}')

    lines += ["", "(Read-only status digest. It does not fix anything.)"]
    return "\n".join(lines)


def build_text(header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, *args):
    if len(args) == 1:
        review_rows, review_meta, review_threshold, window_min = [], {"error": None}, DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD, args[0]
    elif len(args) == 4:
        review_rows, review_meta, review_threshold, window_min = args
    else:
        raise TypeError("build_text expects window_min or review_rows, review_meta, review_threshold, window_min")
    model = build_report_view_model(
        header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, window_min,
        review_rows, review_meta, review_threshold,
    )
    return build_text_view(model)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--window-min", type=int, default=360)
    p.add_argument("--header-file", help="JSON: when, health, model, auth, work_stoppage, needs_attention")
    p.add_argument("--out-html", default="/tmp/hermes_report.html")
    p.add_argument("--out-text", default="/tmp/hermes_report.txt")
    p.add_argument("--out-subject", default="/tmp/hermes_report_subject.txt")
    p.add_argument("--served-ledger", default=SERVED_LEDGER_DEFAULT)
    p.add_argument("--clickup-team-id", default=os.environ.get("CLICKUP_TEAM_ID", DEFAULT_CLICKUP_TEAM_ID))
    p.add_argument("--review-backlog-alert-threshold", type=int,
                   default=int(os.environ.get("HERMES_REVIEW_BACKLOG_ALERT_THRESHOLD", DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD)),
                   help="Prominent alert threshold for workspace-wide review backlog (default: 25).")
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
    review_rows, review_meta = fetch_workspace_review_queue(team_id=args.clickup_team_id)
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
            "total_cost": None, "writer_total_cost": None,
            "today_cost": None, "previous_window_cost": None, "cost_delta": None,
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
            "writer_total_cost": 0.0,
            "today_cost": sum(float(r.get("cost_usd") or 0) for r in today_rows),
            "previous_window_cost": sum(float(r.get("cost_usd") or 0) for r in previous_window_rows),
            "cost_delta": -sum(float(r.get("cost_usd") or 0) for r in previous_window_rows),
            "provider_rows": [], "providers_n": 0, "runs_n": 0, "drift_n": 0, "top_drift_model": None,
        }
    else:
        spend = summarize_spend(window_rows, today_rows, previous_window_rows)
        spend["empty"] = False
        spend["error"] = None
    spend.update(load_guard_tracked_spend())

    scoreboard = build_scoreboard(
        hermes_rows, snap_tasks, hermes_meta, work_counts["completed"], review_rows,
        review_error=bool(review_meta.get("error")),
    )

    # Deterministic verdict OVERWRITES whatever the header-file guessed for
    # work_stoppage — see "work-stoppage verdict" section above. The cron
    # skill's header JSON no longer needs to (and can no longer accidentally)
    # assert STALLED itself.
    header = dict(header or {})
    header["work_stoppage"] = compute_work_stoppage(
        hermes_meta.get("ready", 0), work_counts["completed"], scoreboard["in_progress"], work_counts,
    )

    alerts = build_alerts(hermes_rows, snap_tasks_by_id, header)
    review_alert = build_review_backlog_alert(review_rows, review_meta, args.review_backlog_alert_threshold)
    if review_alert:
        alerts.insert(0, review_alert)
    model = build_report_view_model(
        header, scoreboard, spend, alerts, hermes_rows, hermes_meta, work_rows, args.window_min,
        review_rows, review_meta, args.review_backlog_alert_threshold,
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
        "workspace_review_queue_n": len(review_rows) if not review_meta.get("error") else None,
        "workspace_review_queue_error": review_meta.get("error"),
        "review_backlog_alert_threshold": args.review_backlog_alert_threshold,
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
        "writer_served_cost_usd": (round(spend["writer_total_cost"], 4) if spend.get("writer_total_cost") is not None else None),
        "guard_tracked_spend_usd": (round(spend["guard_total_cost"], 4) if spend.get("guard_total_cost") is not None else None),
        "guard_tracked_spend_error": spend.get("guard_error"),
        "spend_ledger_error": spend.get("error"),
        **model["counts"],
        "providers_n": spend["providers_n"],
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
