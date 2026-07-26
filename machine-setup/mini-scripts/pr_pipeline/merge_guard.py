#!/usr/bin/env python3
"""merge_guard.py — mechanical pre_tool_call gate that refuses PR self-merge.

Wired as a Hermes `pre_tool_call` shell hook (config.yaml `hooks:`), this runs
BEFORE the executor's tool executes. Hermes calls
get_pre_tool_call_block_message() before dispatching ANY tool
(agent/tool_executor.py:346 & :837); a {"decision":"block"} here makes the tool
short-circuit to {"error": reason} and the command NEVER runs. The LLM cannot
bypass it — enforced in code, not prose.

WIRE PROTOCOL (shell_hooks.py):
  stdin  : {"hook_event_name","tool_name","tool_input":{...},"session_id","cwd","extra"}
  stdout : {"decision":"block","reason":"..."}  to block, or empty == allow.

POLICY: block a merge attempt when VALIDATE_SHADOW is truthy OR
  HERMES_AUTONOMOUS_MERGE is NOT in {1,true,yes,on} (default-deny). Else allow.

FAIL CLOSED: any internal error while handling a *matched merge* still blocks.
The only fail-open is the runtime's own hook timeout/crash handling
(shell_hooks.py:439-444), so this guard is tiny, stdlib-only, no network/
subprocess (finishes far under the 10s timeout).

COVERAGE (matched against the command STRING, argv-independent — catches bare
`gh`, absolute `/opt/homebrew/bin/gh`, the `/tmp/gh_with_token.sh` wrapper, the
`gh api .../merge` REST form, raw `curl ... pulls/N/merge`, the inline-python
`subprocess.run(['gh','pr','merge',...])` list form, and any inline command that
hits the `pulls/<n>/merge` REST path) PLUS the MCP merge tool by tool_name.

RESIDUAL (accepted, documented): an LLM that authors a SEPARATE script file
(`python3 /tmp/x.py`) whose body does a urllib PUT to the merge endpoint is not
visible to a command-string matcher. That is a deliberate-obfuscation path, not
the executor's documented ship-loop behavior (which literally runs `gh pr
merge`); the realistic surface is fully closed. Closing the residual would need
egress filtering or revoking the token's Contents:write scope (breaks push).
"""
import json
import os
import re
import sys

# Import the verdict store without an optional-import escape hatch: every
# deployed guard has this canonical sibling, and an import failure must be
# visible as a fail-closed guard error rather than silently changing modules.
if __package__:
    from . import validator_verdict
else:
    import validator_verdict

BLOCK_REASON = (
    "MERGE BLOCKED by merge_guard (mechanical pre_tool_call gate). "
    "Autonomous PR merge is DISABLED: VALIDATE_SHADOW is truthy or "
    "HERMES_AUTONOMOUS_MERGE is not enabled. This is intentional, NOT a "
    "transient error — do NOT retry, do NOT switch tools, do NOT call the "
    "absolute-path gh or self-mint a token and curl the merge API; those are "
    "gated too. Leave the PR OPEN for the human/validator to merge. If the PR "
    "is ALREADY merged, treat that as SUCCESS and advance the ClickUp task (do "
    "not loop). To enable real merges, set HERMES_AUTONOMOUS_MERGE=1 and "
    "VALIDATE_SHADOW=false in Doppler claude-code/dev."
)

_TRUTHY = {"1", "true", "yes", "on"}

# --- Merge-intent matchers (anchored on the pr+merge SUBCOMMAND, not the word) -
# gh [global flags] pr merge ...   (bare gh / absolute gh / GH_TOKEN=.. gh)
_RE_GH_PR_MERGE = re.compile(r"(?:^|[\s;&|/'\"`(])gh\b(?:\s+-{1,2}\S+)*\s+pr\s+merge\b")
# /tmp/gh_with_token.sh [flags] pr merge ...   (the runtime token wrapper)
_RE_WRAPPER_MERGE = re.compile(r"gh_with_token\.sh\b(?:\s+-{1,2}\S+)*\s+pr\s+merge\b")
# gh api ... pulls/<n>/merge   OR   gh api ... /merges
_RE_GH_API_MERGE = re.compile(r"\bgh\b[^\n]*?\bapi\b[^\n]*?(?:pulls/\d+/merge\b|/merges\b)")
# raw curl ... pulls/<n>/merge   (self-minted-token bypass)
_RE_CURL_MERGE = re.compile(r"\bcurl\b[^\n]*?pulls/\d+/merge\b")
# ANY command that hits the REST merge path (inline python urllib/http, etc.)
_RE_ANY_REST_MERGE = re.compile(r"pulls/\d+/merge\b")
# subprocess list form: ['gh','pr','merge', ...] / ("gh","pr","merge")
_RE_PYLIST_MERGE = re.compile(r"""['"]gh['"]\s*,\s*['"]pr['"]\s*,\s*['"]merge['"]""")

_MERGE_TOOL_NAMES = {
    "mcp__plugin_github_github__merge_pull_request",
    "merge_pull_request",
}

VERDICT_BLOCK_REASON = (
    "MERGE BLOCKED by merge_guard: the PR validator has no fresh PASS verdict on "
    "record for this PR ({why}). Autonomous merge requires a PASS verdict written "
    "by hermes-pr-validate for the exact PR head. Leave the PR OPEN; the validator "
    "(woken by review-poll-gate on the needs-validation tag) will record a verdict, "
    "or a human can merge. Do NOT retry or switch tools — the verdict gate is "
    "mechanical and applies to every merge path."
)


def _extract_repo_pr(tool_name, tool_input, cmd):
    """Best-effort (owner/repo, pr_number) from an MCP call or command string.
    Returns (repo, pr) or (None, None) if it can't be determined."""
    if tool_name in _MERGE_TOOL_NAMES and isinstance(tool_input, dict):
        owner = tool_input.get("owner")
        repo = tool_input.get("repo")
        num = (tool_input.get("pullNumber") or tool_input.get("pull_number")
               or tool_input.get("number"))
        if owner and repo and num:
            try:
                return f"{owner}/{repo}", int(num)
            except (TypeError, ValueError):
                return None, None
        return None, None
    if cmd:
        m = re.search(r"repos/([^/\s'\"]+/[^/\s'\"]+)/pulls/(\d+)/merge", cmd)
        if m:
            return m.group(1), int(m.group(2))
        repo = None
        rm = re.search(r"--repo[=\s]+(['\"]?)([^\s'\"]+)\1", cmd)
        if rm:
            repo = rm.group(2)
        pm = re.search(r"\bpr\s+merge\s+(?:-{1,2}\S+(?:[=\s]\S+)?\s+)*?(\d+)\b", cmd)
        pr = int(pm.group(1)) if pm else None
        if repo and pr:
            return repo, pr
    return None, None


def _verdict_gate(tool_name, tool_input, cmd):
    """Return (allowed, reason). A merge needs a fresh PASS verdict for the exact
    PR. Unknown PR / no verdict / BLOCK / stale = fail closed."""
    if validator_verdict is None:
        return False, "verdict store module unavailable (fail-closed)"
    repo, pr = _extract_repo_pr(tool_name, tool_input, cmd)
    if not repo or not pr:
        return False, "could not determine which PR is being merged (fail-closed)"
    try:
        ok, why = validator_verdict.is_pass_fresh(repo, pr)
        if not ok:
            return False, why
        tier = (validator_verdict.verdict_for(repo, pr) or {}).get("tier", "high")
        if not _tier_autonomy_enabled(tier):
            return False, (f"PASS verdict exists but autonomous merge is not enabled "
                           f"for risk tier '{tier}' "
                           f"(set HERMES_AUTONOMOUS_MERGE_{tier.upper()}=1 once graduated)")
        return True, f"PASS + tier '{tier}' autonomy enabled"
    except Exception as e:
        return False, f"verdict check error: {e!r} (fail-closed)"


def _is_merge_command(cmd):
    """True iff the command string is an attempt to merge a PR.

    Anchored on the pr+merge subcommand pair / REST merge path / py-list form.
    Does NOT match a bare 'merge' word, so `git merge`, `gh pr create
    --title '...merge...'`, `gh pr comment --body '...merge'`, and
    `gh pr view --json mergeable` are NOT flagged.
    """
    if not cmd:
        return False
    return bool(
        _RE_GH_PR_MERGE.search(cmd)
        or _RE_WRAPPER_MERGE.search(cmd)
        or _RE_GH_API_MERGE.search(cmd)
        or _RE_CURL_MERGE.search(cmd)
        or _RE_ANY_REST_MERGE.search(cmd)
        or _RE_PYLIST_MERGE.search(cmd)
    )


def _shadow():
    # The vendored trust-boundary snapshot is shadow-only.  Preserve this
    # mechanical block even when a caller injects VALIDATE_SHADOW=false or an
    # autonomous-merge flag.  Live merge activation is deliberately outside
    # the reconciler's deployment surface.
    return True


def _tier_autonomy_enabled(tier):
    """Per-risk-tier autonomy (graduates independently). The master switch
    HERMES_AUTONOMOUS_MERGE enables ALL tiers; otherwise the per-tier flag
    HERMES_AUTONOMOUS_MERGE_<LOW|MEDIUM|HIGH> must be truthy. Unknown tier is
    treated as HIGH (most conservative)."""
    if (os.environ.get("HERMES_AUTONOMOUS_MERGE", "") or "").strip().lower() in _TRUTHY:
        return True
    key = "HERMES_AUTONOMOUS_MERGE_" + (tier or "high").upper()
    return (os.environ.get(key, "") or "").strip().lower() in _TRUTHY


def _block(reason=BLOCK_REASON):
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.stdout.flush()
    sys.exit(0)


def _allow():
    sys.exit(0)  # empty stdout == no-op (tool proceeds)


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        # Unparseable payload: cannot tell if it's a merge; must not block
        # unrelated tools. The real merge attempt re-hits this guard parseably.
        _allow()

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    # MCP merge tool: matched on tool_name alone. Two gates, both required:
    # (1) env autonomy switch, (2) a fresh PASS verdict for THIS PR.
    if tool_name in _MERGE_TOOL_NAMES:
        try:
            if _shadow():
                _block()
            ok, why = _verdict_gate(tool_name, tool_input, None)
            if ok:
                _allow()
            _block(VERDICT_BLOCK_REASON.format(why=why))
        except Exception:
            _block()  # FAIL CLOSED for a matched merge

    # terminal tool: inspect the command string.
    if tool_name == "terminal":
        cmd = tool_input.get("command")
        if not isinstance(cmd, str):
            _allow()
        try:
            if not _is_merge_command(cmd):
                _allow()
            if _shadow():
                _block()
            ok, why = _verdict_gate(tool_name, tool_input, cmd)
            if ok:
                _allow()
            _block(VERDICT_BLOCK_REASON.format(why=why))
        except Exception:
            # Reaching here implies _is_merge_command matched; FAIL CLOSED.
            _block()

    _allow()  # any other tool: not our business


if __name__ == "__main__":
    main()
