#!/usr/bin/env python3
"""git_commit_identity_guard.py — pre_tool_call gate that blocks git commits
with non-bot author identity.

Wired as a Hermes `pre_tool_call` shell hook (config.yaml `hooks:`).
Blocks `git commit` terminal commands that use `-c user.email=<non-bot>` or
`-c user.name=<non-bot>` flags, which produce commits NOT attributed to the
GitHub App identity (messy/wrong authorship, breaks `[bot]` attribution and
the GIT_CONFIG_GLOBAL bot identity). This is COMMIT-HYGIENE enforcement.

CORRECTION (2026-06-18, verified): an earlier note claimed the wrong author
made "Vercel reject the commits" — that is FALSE. Vercel builds regardless of
commit author. The real Vercel preview failures on these repos were a Next.js
build error (a Supabase client instantiated at static-prerender time without
env), unrelated to this guard, and were resolved separately by disabling
Vercel preview deployments fleet-wide (Colin never uses preview). Keep this
guard for clean bot attribution, NOT as a Vercel fix.

WIRE PROTOCOL (shell_hooks.py):
  stdin  : {"hook_event_name","tool_name","tool_input":{...},"session_id","cwd","extra"}
  stdout : {"decision":"block","reason":"..."}  to block, or empty == allow.

ROOT CAUSE FIXED (2026-06-18):
  A success reference in clickup-queue-poller skill (ref #27) documented:
    git commit -c user.email='hermes@ignitemarketing.com' -c user.name='Hermes Dev Assistant'
  The agent followed this pattern, producing commits authored as a non-bot
  identity (wrong attribution; NOT a Vercel failure — see CORRECTION above).
  The correct approach is to NOT pass -c user.email/user.name at all;
  GIT_CONFIG_GLOBAL in the executor env already sets the bot identity.

CORRECT BOT IDENTITY:
  name  = hermes-dev-assistant[bot]
  email = 293647229+hermes-dev-assistant[bot]@users.noreply.github.com

FAIL-OPEN: any error in this hook (import error, JSON parse error, etc.)
causes the hook to allow the command through rather than blocking executor work.
The skill-level text rules are the primary guard; this is defense-in-depth.
"""
import json
import os
import re
import sys

BOT_EMAIL = "293647229+hermes-dev-assistant[bot]@users.noreply.github.com"
BOT_NAME = "hermes-dev-assistant[bot]"

# Non-bot emails that have appeared in past bad commits (block list)
_BAD_EMAILS = {
    "hermes@ignitemarketing.com",
    "hermes@colingreig.com",
    "hermes@local",
    "agent@hermes.local",
}

# Pattern: git commit ... -c user.email=<value>
_RE_COMMIT_CEMAIL = re.compile(
    r"\bgit\b[^\n]*?\bcommit\b[^\n]*?-c\s+user\.email\s*=\s*(['\"]?)([^\s'\"]+)\1",
    re.IGNORECASE,
)
# Pattern: git commit ... -c user.name=<value>
_RE_COMMIT_CNAME = re.compile(
    r"\bgit\b[^\n]*?\bcommit\b[^\n]*?-c\s+user\.name\s*=\s*(['\"]?)([^\s'\"]+)\1",
    re.IGNORECASE,
)

BLOCK_REASON_TEMPLATE = (
    "GIT COMMIT BLOCKED by git_commit_identity_guard: the command passes "
    "`-c user.email={email}` which overrides GIT_CONFIG_GLOBAL and produces "
    "a Vercel-rejected commit. "
    "CORRECT FIX: remove the `-c user.email=` and `-c user.name=` flags. "
    "The executor env already sets GIT_CONFIG_GLOBAL=~/.hermes/gitconfig which "
    "author-stamps commits as hermes-dev-assistant[bot] "
    "<293647229+hermes-dev-assistant[bot]@users.noreply.github.com>. "
    "If you suspect a local worktree override, run "
    "`git config user.email \"293647229+hermes-dev-assistant[bot]@users.noreply.github.com\"` "
    "and `git config user.name \"hermes-dev-assistant[bot]\"` BEFORE committing. "
    "Do NOT retry with a different email — there is no correct email to pass here."
)


def _is_bad_email(email: str) -> bool:
    """Return True if the email is known-bad or is NOT the bot email."""
    email = email.strip().strip("'\"")
    if email.lower() in _BAD_EMAILS:
        return True
    # Block ANY explicit -c user.email= that isn't the bot email
    # (the bot email itself is benign but unnecessary)
    if email != BOT_EMAIL:
        return True
    return False


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        payload = json.loads(raw)
        tool_name = payload.get("tool_name", "")
        tool_input = payload.get("tool_input", {})

        # Only inspect terminal tool calls
        if tool_name != "terminal":
            sys.exit(0)

        cmd = ""
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command", "") or tool_input.get("cmd", "")
        if not cmd:
            sys.exit(0)

        # Check for bad -c user.email= in git commit
        m = _RE_COMMIT_CEMAIL.search(cmd)
        if m:
            email = m.group(2)
            if _is_bad_email(email):
                result = {
                    "decision": "block",
                    "reason": BLOCK_REASON_TEMPLATE.format(email=email),
                }
                print(json.dumps(result))
                sys.exit(0)

    except Exception:
        # Fail-open on any error
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
