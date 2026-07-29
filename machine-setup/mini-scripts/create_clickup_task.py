#!/usr/bin/env python3
"""
create_clickup_task.py — reusable ClickUp task CLI (create + tag management).

WHY THIS EXISTS (2026-06-13): every session that needed to FILE a ClickUp task
hand-rolled a one-off script and repeatedly hit the platform `write_file`
truncation pitfall — a line shaped like `TOKEN=os.environ[...]` adjacent to a
bracket gets silently rewritten to `TOKEN=*** ...`, producing a SyntaxError at
runtime (observed live in the executor logs again this morning). This tool
removes the need to re-discover the workaround: it uses the proven
`subprocess.run + curl` shape, reads the token from the environment AT RUNTIME
(so no env-var assignment literal ever appears in a file), and is parameterised
entirely via flags / env vars.

It is the operator-grade companion to the skill template at
`~/.hermes/skills/clickup-queue-poller/scripts/create_task.py` (which stays as
the minimal copy-me example). This one adds argparse, parent-list
auto-resolution (avoids the ITEM_137 "Parent not child of list" trap), tag
add/remove, and response verification.

Token: CLICKUP_API_TOKEN (Doppler-injected; already present in the gateway env).

USAGE
  # Create a task (body read from a file — the safe shape for non-trivial text):
  python3 create_clickup_task.py create \
      --list 901714465284 --name "Fix the thing" --body /tmp/body.md \
      --tags "agent-ready,clickup" --priority 3

  # Create a SUBTASK — omit --list and it is resolved from the parent's list
  # automatically (this is what prevents ITEM_137):
  python3 create_clickup_task.py create \
      --parent 86e1uxqm2 --name "Phase 3 deliverable" --body /tmp/body.md \
      --tags "agent-ready,phase-3"

  # Tag management on an existing task:
  python3 create_clickup_task.py add-tag 86e1vw79j agent-ready
  python3 create_clickup_task.py rm-tag  86e1vw79j agent-ready

ENV-VAR FALLBACKS (flags win): LIST_ID, TASK_NAME, BODY_PATH, PARENT_ID,
TAGS_CSV (or TAGS), PRIORITY.

PRIORITY: 1=urgent, 2=high, 3=normal (default), 4=low.
"""
import argparse
import json
import os
import subprocess
import sys

API = "https://api.clickup.com/api/v2"


def _token():
    value = None
    try:
        from agent import lazy_secret_resolver
        value = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        value = None
    if not value:
        value = os.environ.get("CLICKUP_API_TOKEN", "")
    tok = (value or "").strip()
    if not tok:
        sys.exit("CLICKUP_API_TOKEN not set in env — cannot call ClickUp")
    return tok


def _curl(method, url, token, payload=None):
    """Run curl and return (parsed_json_or_None, raw_stdout)."""
    cmd = ["curl", "-s", "-X", method,
           "-H", f"Authorization: {token}",
           "-H", "Content-Type: application/json"]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        sys.exit(f"curl failed (rc={res.returncode}): {res.stderr.strip()}")
    raw = res.stdout
    try:
        return json.loads(raw), raw
    except Exception:
        return None, raw


def _die_on_api_error(resp, raw, context):
    if resp is None:
        sys.exit(f"{context}: non-JSON response: {raw[:300]}")
    if isinstance(resp, dict) and resp.get("err"):
        ecode = resp.get("ECODE", "")
        hint = ""
        if ecode == "ITEM_137":
            hint = ("\nHINT: the --parent task lives in a DIFFERENT list than "
                    "--list. Omit --list (it is auto-resolved from the parent) "
                    "or point --list at the parent's list id.")
        sys.exit(f"{context}: API error {ecode}: {resp.get('err')}{hint}")


def _parent_list_id(parent_id, token):
    resp, raw = _curl("GET", f"{API}/task/{parent_id}", token)
    _die_on_api_error(resp, raw, f"resolving parent {parent_id}")
    lst = (resp.get("list") or {})
    if not lst.get("id"):
        sys.exit(f"parent {parent_id} has no resolvable list")
    return lst["id"], lst.get("name", "")


def _list_statuses(list_id, token):
    """Return list of {name, type} dicts valid in this list, or sys.exit on error.
    Also enforces the archived-list rule: if the list (or its parent folder) is
    archived, sys.exit with a clear error rather than waste a POST on a 4xx."""
    resp, raw = _curl("GET", f"{API}/list/{list_id}", token)
    _die_on_api_error(resp, raw, f"resolving list {list_id}")
    if resp.get("archived"):
        sys.exit(f"create: refusing to post to archived list {list_id} "
                 f"('{resp.get('name')}'). Unarchive first or pick a different list.")
    folder = resp.get("folder") or {}
    if folder.get("archived"):
        sys.exit(f"create: refusing to post to list {list_id} — its parent folder "
                 f"{folder.get('id')} ('{folder.get('name')}') is archived.")
    return resp.get("statuses") or []


def _default_status_for(statuses, requested=None):
    """Pick a status. If requested is given, require it to exist. Otherwise
    prefer 'to do' (most common), then any 'open', then 'backlog', then the
    first one alphabetically. Returns the picked name.
    Accepts status dicts with either `name` or `status` key (ClickUp API uses
    `status` on /list responses, `name` on some other endpoints)."""
    if not statuses:
        return "to do"
    def _n(s): return s.get("name") or s.get("status")
    names = [_n(s) for s in statuses if _n(s)]
    if requested:
        if requested not in names:
            sys.exit(f"create: --status '{requested}' is not a valid status "
                     f"for this list. Valid: {names}")
        return requested
    # Auto-pick preference order
    for prefer in ["to do", "backlog", "deferred", "open"]:
        if prefer in names:
            return prefer
    # Fall back: first open/unstarted, else first
    for s in statuses:
        if s.get("type") in ("open", "unstarted"):
            return _n(s)
    return names[0]


def _team_members(token):
    """Return [{id, username, email}] for the workspace, or [] on failure.
    Members come from GET /team/{TEAM_ID} -> team.members[].user. If no
    CLICKUP_TEAM_ID is set, list authorized teams and flatten their members."""
    team_id = os.environ.get("CLICKUP_TEAM_ID", "").strip()
    if team_id:
        resp, _ = _curl("GET", f"{API}/team/{team_id}", token)
        members = ((resp or {}).get("team") or {}).get("members") or []
    else:
        resp, _ = _curl("GET", f"{API}/team", token)
        members = []
        for t in ((resp or {}).get("teams") or []):
            members += t.get("members") or []
    out = []
    for m in members:
        u = m.get("user") or {}
        if u.get("id"):
            out.append({"id": u["id"],
                        "username": (u.get("username") or "").strip(),
                        "email": (u.get("email") or "").strip()})
    return out


def _match_member(name, members):
    """Match a human name/handle to member id(s). Returns (ids, candidates):
    a 1-element ids list on a confident single match, else ([], candidate
    descriptions) when ambiguous so the caller can escalate rather than guess.
    Tiers (first non-empty wins): exact username/email, email local-part,
    first-name token, substring."""
    q = (name or "").strip().lower()
    if not q:
        return [], []
    def uname(m): return m["username"].lower()
    def local(m): return m["email"].split("@", 1)[0].lower()
    tiers = [
        [m for m in members if uname(m) == q or m["email"].lower() == q],
        [m for m in members if local(m) == q],
        [m for m in members if q in uname(m).split()],
        [m for m in members if q in uname(m) or q in local(m)],
    ]
    for hits in tiers:
        if len(hits) == 1:
            return [hits[0]["id"]], []
        if len(hits) > 1:
            return [], [f'{m["username"]} <{m["email"]}> (id {m["id"]})' for m in hits]
    return [], []


def _resolve_assignee_ids(args, token):
    """Build the assignees list: explicit numeric ids from --assignees win;
    --assignee-name is resolved to a single id via the roster. A name that is
    ambiguous or matches nobody is a HARD ERROR (never a silent punt) — the
    email-triage agent must then escalate to Colin per SKILL §9."""
    ids = []
    raw_ids = args.assignees if args.assignees is not None else os.environ.get("ASSIGNEES", "")
    for tok in str(raw_ids).split(","):
        tok = tok.strip()
        if tok.isdigit():
            ids.append(int(tok))
    name = args.assignee_name or os.environ.get("ASSIGNEE_NAME")
    if name:
        matched, candidates = _match_member(name, _team_members(token))
        if matched:
            ids += matched
        elif candidates:
            sys.exit("create: --assignee-name '%s' is AMBIGUOUS — matches:\n  %s\n"
                     "Pass --assignees <id> explicitly, or escalate per SKILL §9."
                     % (name, "\n  ".join(candidates)))
        else:
            sys.exit("create: --assignee-name '%s' matched NO workspace member. "
                     "Do NOT punt this silently: omit the assignee and escalate to "
                     "Colin (@mention + assign the intake to him per SKILL §9), "
                     "or pass a correct --assignees <id>." % name)
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _parse_due(s):
    """Accept a due date as YYYY-MM-DD (also Y/M/D, M/D/Y) or raw epoch ms.
    Returns epoch ms or None."""
    if not s:
        return None
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    import datetime
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    sys.exit(f"create: --due '{s}' not understood (use YYYY-MM-DD or epoch ms)")


def cmd_create(args, token):
    name = args.name or os.environ.get("TASK_NAME")
    if not name:
        sys.exit("create: --name (or TASK_NAME) is required")
    body_path = args.body or os.environ.get("BODY_PATH")
    if not body_path:
        sys.exit("create: --body <file> (or BODY_PATH) is required")
    try:
        with open(body_path) as f:
            description = f.read()
    except OSError as e:
        sys.exit(f"create: cannot read body file {body_path}: {e}")

    parent = args.parent or os.environ.get("PARENT_ID") or ""
    list_id = args.list or os.environ.get("LIST_ID") or ""
    tags_csv = args.tags if args.tags is not None else os.environ.get("TAGS_CSV", os.environ.get("TAGS", ""))
    priority = int(args.priority if args.priority is not None else os.environ.get("PRIORITY", "3"))
    tags = [t.strip() for t in tags_csv.split(",") if t.strip()]

    # Resolve the list. A subtask MUST be created in its parent's list
    # (ITEM_137 trap). If a parent is given and no list, resolve it.
    if parent and not list_id:
        list_id, list_name = _parent_list_id(parent, token)
        print(f"[create] resolved parent {parent} -> list {list_id} ({list_name})")
    elif parent and list_id:
        plist, pname = _parent_list_id(parent, token)
        if plist != list_id:
            print(f"[create] WARNING: --list {list_id} != parent's list {plist} "
                  f"({pname}); ClickUp will reject with ITEM_137. Using parent's list.",
                  file=sys.stderr)
            list_id = plist
    if not list_id:
        sys.exit("create: --list (or LIST_ID), or a --parent to resolve it from, is required")

    statuses = _list_statuses(list_id, token)
    chosen_status = _default_status_for(statuses, args.status or os.environ.get("STATUS"))

    # ClickUp renders `markdown_content` as rich text; the older `description`
    # field shows the raw markdown literally (the 2026-06-17 "# and ** show as
    # text" bug on child 86e1xwxh1). Always use markdown_content.
    payload = {"name": name, "markdown_content": description, "status": chosen_status,
               "priority": priority, "tags": tags}
    if parent:
        payload["parent"] = parent

    # Resolve assignees + due date from the email body instead of punting them
    # back to Colin as "open questions" (the 2026-06-17 silly-questions bug).
    assignee_ids = _resolve_assignee_ids(args, token)
    if assignee_ids:
        payload["assignees"] = assignee_ids
    due_ms = _parse_due(args.due or os.environ.get("DUE"))
    if due_ms is not None:
        payload["due_date"] = due_ms

    if args.dry_run:
        print(f"[dry-run] would POST to list {list_id}; payload (body elided):")
        preview = dict(payload, markdown_content=f"<{len(description)} chars>")
        print(json.dumps(preview, indent=2))
        return

    resp, raw = _curl("POST", f"{API}/list/{list_id}/task", token, payload)
    _die_on_api_error(resp, raw, "create")

    got_tags = [t.get("name") for t in (resp.get("tags") or [])]
    dropped = [t for t in tags if t not in got_tags]
    print("id:", resp.get("id"))
    print("url:", resp.get("url"))
    print("status:", (resp.get("status") or {}).get("status"))
    print("list:", (resp.get("list") or {}).get("name"))
    print("parent:", resp.get("parent") or "(none)")
    print("assignees:", [a.get("id") for a in (resp.get("assignees") or [])] or "(none)")
    if resp.get("due_date"):
        print("due_date:", resp.get("due_date"))
    print("tags:", got_tags)
    if dropped:
        print(f"WARNING: tags silently dropped by ClickUp: {dropped} "
              "(known per-list cap quirk — re-add individually with add-tag)",
              file=sys.stderr)

    # Child-landing verification (2026-06-18): confirm the task actually landed in
    # the intended list. A list_id typo / stale id silently files the task in the
    # wrong place and the misroute is invisible until review. Make it loud + exit
    # non-zero so the caller (e.g. the triage agent) catches it and moves the task.
    got_list = str((resp.get("list") or {}).get("id") or "")
    if got_list and got_list != str(list_id):
        print(f"ERROR: task {resp.get('id')} landed in list {got_list} "
              f"('{(resp.get('list') or {}).get('name')}'), NOT the intended "
              f"list {list_id}. The task WAS created — move it to the correct "
              "list; do not assume routing succeeded.", file=sys.stderr)
        sys.exit(3)


def cmd_add_tag(args, token):
    resp, raw = _curl("POST", f"{API}/task/{args.task_id}/tag/{args.tag}", token)
    # Tag endpoints return {} on success; only error if an explicit err is set.
    if isinstance(resp, dict) and resp.get("err"):
        _die_on_api_error(resp, raw, "add-tag")
    print(f"added tag '{args.tag}' to {args.task_id}")


def cmd_rm_tag(args, token):
    # DELETE /task/{id}/tag/{name} is the ONLY reliable removal — PUT with a
    # tags array returns 200 but silently no-ops (per SKILL.md error table).
    resp, raw = _curl("DELETE", f"{API}/task/{args.task_id}/tag/{args.tag}", token)
    if isinstance(resp, dict) and resp.get("err"):
        _die_on_api_error(resp, raw, "rm-tag")
    print(f"removed tag '{args.tag}' from {args.task_id}")


def main():
    p = argparse.ArgumentParser(description="ClickUp task CLI (create + tags).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a task (or subtask)")
    c.add_argument("--list", help="list id (omit to resolve from --parent)")
    c.add_argument("--name", help="task name (or TASK_NAME)")
    c.add_argument("--body", help="path to markdown body file (or BODY_PATH)")
    c.add_argument("--parent", help="parent task id (or PARENT_ID)")
    c.add_argument("--tags", help="comma-separated tags (or TAGS_CSV/TAGS)")
    c.add_argument("--assignees", help="comma-separated ClickUp user ids to assign (or ASSIGNEES)")
    c.add_argument("--assignee-name", help="human name/handle (e.g. 'Nawid') resolved to a user id via the workspace roster (or ASSIGNEE_NAME)")
    c.add_argument("--due", help="due date as YYYY-MM-DD or epoch ms (or DUE)")
    c.add_argument("--priority", help="1=urgent 2=high 3=normal 4=low (or PRIORITY)")
    c.add_argument("--status", help="status name to create the task in; defaults to auto-pick (or STATUS env)")
    c.add_argument("--dry-run", action="store_true",
                   help="resolve the list + show the payload, but do not POST")
    c.set_defaults(func=cmd_create)

    a = sub.add_parser("add-tag", help="add a tag to an existing task")
    a.add_argument("task_id")
    a.add_argument("tag")
    a.set_defaults(func=cmd_add_tag)

    r = sub.add_parser("rm-tag", help="remove a tag from an existing task")
    r.add_argument("task_id")
    r.add_argument("tag")
    r.set_defaults(func=cmd_rm_tag)

    args = p.parse_args()
    args.func(args, _token())


if __name__ == "__main__":
    main()
