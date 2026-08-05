---
name: github-pr-workflow
description: "GitHub PR lifecycle: branch, commit, open, CI, merge."
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Pull-Requests, CI/CD, Git, Automation, Merge]
    related_skills: [github-auth, github-code-review]
---

# GitHub Pull Request Workflow

Complete guide for managing the PR lifecycle. Each section shows the `gh` way first, then the `git` + `curl` fallback for machines without `gh`.

## Prerequisites

- Authenticated with GitHub (see `github-auth` skill)
- Inside a git repository with a GitHub remote

### Quick Auth Detection

```bash
# Determine which method to use throughout this workflow
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  # Ensure we have a token for API calls
  if [ -z "$GITHUB_TOKEN" ]; then
    if _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(grep "github.com" ~/.git-credentials 2>/dev/null | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|')
    fi
  fi
fi
echo "Using: $AUTH"
```

### Extracting Owner/Repo from the Git Remote

Many `curl` commands need `owner/repo`. Extract it from the git remote:

```bash
# Works for both HTTPS and SSH remote URLs
REMOTE_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
OWNER=$(echo "$OWNER_REPO" | cut -d/ -f1)
REPO=$(echo "$OWNER_REPO" | cut -d/ -f2)
echo "Owner: $OWNER, Repo: $REPO"
```

---

## 1. Branch Creation

This part is pure `git` — identical either way:

```bash
# Make sure you're up to date
git fetch origin
git checkout main && git pull origin main

# Create and switch to a new branch
git checkout -b feat/add-user-authentication
```

Branch naming conventions:
- `feat/description` — new features
- `fix/description` — bug fixes
- `refactor/description` — code restructuring
- `docs/description` — documentation
- `ci/description` — CI/CD changes

## 2. Making Commits

Use the agent's file tools (`write_file`, `patch`) to make changes, then commit:

```bash
# Stage specific files
git add src/auth.py src/models/user.py tests/test_auth.py

# Commit with a conventional commit message
git commit -m "feat: add JWT-based user authentication

- Add login/register endpoints
- Add User model with password hashing
- Add auth middleware for protected routes
- Add unit tests for auth flow"
```

Commit message format (Conventional Commits):
```
type(scope): short description

Longer explanation if needed. Wrap at 72 characters.
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `ci`, `chore`, `perf`

## 3. Pushing and Creating a PR

### Mandatory task-level reuse check

When the work comes from ClickUp, branch-level idempotency is not enough: an
executor retry normally has a new cycle branch, so `gh pr create` can succeed
again for the same task. Before creating a branch **or** opening a PR, build an
**exhaustive, tri-state** inventory of every open PR. Use the REST pagination
path (a fixed `gh pr list` limit silently truncates larger repos):

```bash
TASK_ID="${CLICKUP_TASK_ID:?set the ClickUp task id}"
ACTIVE_BOT_PREFIXES="${ACTIVE_BOT_PREFIXES:-agent/,hermes/}"
OWNER_REPO="${OWNER_REPO:?set owner/repo}"

if ! gh api --paginate --slurp \
  "repos/$OWNER_REPO/pulls?state=open&per_page=100" > /tmp/open-pr-pages.json; then
  echo "open-PR inventory UNKNOWN; refusing PR creation" >&2
  exit 1
fi

python3 - "$TASK_ID" "$ACTIVE_BOT_PREFIXES" /tmp/open-pr-pages.json <<'PY'
import json, re, sys

target = sys.argv[1].lower()
prefixes = tuple(p for p in sys.argv[2].split(",") if p)
marker = re.compile(r"<!--\s*clickup-task-id:\s*(86e[0-9a-z]{5,8})\s*-->", re.I)
fallback = re.compile(r"\b(86e[0-9a-z]{5,8})\b", re.I)

def identity(*texts):
    # Canonical markers have precedence. Only when none exist may free-text
    # ids in branch/title/body establish identity.
    marked = {m.lower() for text in texts for m in marker.findall(str(text or ""))}
    if len(marked) == 1:
        return "unique", marked.pop()
    if len(marked) > 1:
        return "ambiguous", None
    ids = {m.lower() for text in texts for m in fallback.findall(str(text or ""))}
    if len(ids) == 1:
        return "unique", ids.pop()
    return ("ambiguous" if ids else "absent"), None

try:
    pages = json.load(open(sys.argv[3], encoding="utf-8"))
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("malformed pagination shape")
    matches = []
    for page in pages:
        for pr in page:
            if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
                raise ValueError("malformed PR row")
            head = pr.get("head")
            user = pr.get("user")
            if not isinstance(head, dict) or not isinstance(head.get("ref"), str):
                raise ValueError("malformed PR head")
            if not isinstance(user, dict) or not isinstance(user.get("login"), str):
                raise ValueError("malformed PR author")
            status, task_id = identity(head["ref"], pr.get("title"), pr.get("body"))
            login = user["login"].lower()
            is_bot = login in {
                "app/hermes-dev-assistant",
                "hermes-dev-assistant",
                "hermes-dev-assistant[bot]",
            }
            if is_bot and head["ref"].startswith(prefixes) and status != "unique":
                raise ValueError(
                    f"{status} task identity on active bot branch {head['ref']}"
                )
            if status == "unique" and task_id == target:
                matches.append((pr["number"], head["ref"]))
except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
    print(f"open-PR inventory UNKNOWN ({exc}); refusing PR creation", file=sys.stderr)
    raise SystemExit(2)

for number, branch in matches:
    print(f"reuse open PR #{number} on {branch}")
raise SystemExit(0 if matches else 3)  # 0=found, 3=authoritatively absent, 2=unknown
PY
case $? in
  0) echo "reuse the PR(s) printed above; do not create another"; exit 0 ;;
  3) echo "task authoritatively absent from exhaustive open-PR inventory" ;;
  *) echo "inventory UNKNOWN; refusing PR creation" >&2; exit 1 ;;
esac
```

The identity rule is deliberate: one canonical
`<!-- clickup-task-id: 86e... -->` marker wins even if unrelated prose contains
another task id; multiple canonical markers are ambiguous. Only when no marker
exists may a unique free-text id across head ref, title, and body identify the
task.

- If a match exists, **reuse that PR and head branch**: put follow-up commits on
  its branch and do not call `gh pr create` again.
- If replacement is genuinely required, explicitly close the prior PR first and
  record why; never leave two open bot PRs for one task.
- Do not delete the superseded branch or invent a second cleanup path. Branch
  and worktree retirement stays with the existing worktree sweep after it proves
  the content is landed or human-approved for retirement.
- Treat inventory as `found | absent | unknown`. API failure, malformed JSON or
  rows, ambiguous identity, or an unidentified Hermes-bot PR under any active
  prefix means **unknown**: fail closed and call no PR-creation API. Only an
  exhaustive, well-formed inventory may return authoritatively absent.

### Push the Branch (same either way)

```bash
git push -u origin HEAD
```

### Create the PR

**With gh:**

```bash
gh pr create \
  --title "feat: add JWT-based user authentication" \
  --body "## Summary
- Adds login and register API endpoints
- JWT token generation and validation

## Test Plan
- [ ] Unit tests pass

Closes #42"
```

Options: `--draft`, `--reviewer user1,user2`, `--label "enhancement"`, `--base develop`

### If `gh pr create` fails: retry + re-auth, never silently strand the push

The branch is already pushed at this point (Section 3 above) — a failed
`gh pr create` does NOT undo that. Treat a non-zero exit as recoverable, not
fatal:

```bash
create_pr() {
  gh pr create --title "$1" --body "$2"
}

attempt=0
until create_pr "$TITLE" "$BODY"; do
  attempt=$((attempt + 1))
  if [ "$attempt" -gt 2 ]; then
    echo "gh pr create failed after $attempt attempts — branch $(git branch --show-current) is pushed but PR-less" >&2
    break
  fi
  # Only auth failures are worth retrying; a validation error (bad base,
  # duplicate PR, etc.) will fail the same way every time.
  if ! gh auth status &>/dev/null; then
    gh auth refresh || true   # or reload the token per the git-only
                              # fallback in the github-auth skill
  fi
  sleep $((attempt * 5))      # backoff: 5s, 10s
done
```

If it still fails after retries: **do not drop it silently.**
- Tag the ClickUp task `needs-validation` so it surfaces on the board instead
  of vanishing.
- Record the pushed-but-PR-less branch so the orphan-PR sweep
  (`scripts/ops/orphan_pr_sweep.py`) can pick it up and open the PR later —
  matching either `agent/*` or `hermes/*` branch prefixes.

**With git + curl:**

```bash
BRANCH=$(git branch --show-current)

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/$OWNER/$REPO/pulls \
  -d "{
    \"title\": \"feat: add JWT-based user authentication\",
    \"body\": \"## Summary\nAdds login and register API endpoints.\n\nCloses #42\",
    \"head\": \"$BRANCH\",
    \"base\": \"main\"
  }"
```

The response JSON includes the PR `number` — save it for later commands.

To create as a draft, add `"draft": true` to the JSON body.

## 4. Monitoring CI Status

### Check CI Status

**With gh:**

```bash
# One-shot check
gh pr checks

# Watch until all checks finish (polls every 10s)
gh pr checks --watch
```

**With git + curl:**

```bash
# Get the latest commit SHA on the current branch
SHA=$(git rev-parse HEAD)

# Query the combined status
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/status \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"Overall: {data['state']}\")
for s in data.get('statuses', []):
    print(f\"  {s['context']}: {s['state']} - {s.get('description', '')}\")"

# Also check GitHub Actions check runs (separate endpoint)
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/check-runs \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for cr in data.get('check_runs', []):
    print(f\"  {cr['name']}: {cr['status']} / {cr['conclusion'] or 'pending'}\")"
```

### Poll Until Complete (git + curl)

```bash
# Simple polling loop — check every 30 seconds, up to 10 minutes
SHA=$(git rev-parse HEAD)
for i in $(seq 1 20); do
  STATUS=$(curl -s \
    -H "Authorization: token $GITHUB_TOKEN" \
    https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/status \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])")
  echo "Check $i: $STATUS"
  if [ "$STATUS" = "success" ] || [ "$STATUS" = "failure" ] || [ "$STATUS" = "error" ]; then
    break
  fi
  sleep 30
done
```

## 5. Auto-Fixing CI Failures

When CI fails, diagnose and fix. This loop works with either auth method.

### Step 1: Get Failure Details

**With gh:**

```bash
# List recent workflow runs on this branch
gh run list --branch $(git branch --show-current) --limit 5

# View failed logs
gh run view <RUN_ID> --log-failed
```

**With git + curl:**

```bash
BRANCH=$(git branch --show-current)

# List workflow runs on this branch
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/actions/runs?branch=$BRANCH&per_page=5" \
  | python3 -c "
import sys, json
runs = json.load(sys.stdin)['workflow_runs']
for r in runs:
    print(f\"Run {r['id']}: {r['name']} - {r['conclusion'] or r['status']}\")"

# Get failed job logs (download as zip, extract, read)
RUN_ID=<run_id>
curl -s -L \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/runs/$RUN_ID/logs \
  -o /tmp/ci-logs.zip
cd /tmp && unzip -o ci-logs.zip -d ci-logs && cat ci-logs/*.txt
```

### Step 2: Fix and Push

After identifying the issue, use file tools (`patch`, `write_file`) to fix it:

```bash
git add <fixed_files>
git commit -m "fix: resolve CI failure in <check_name>"
git push
```

### Step 3: Verify

Re-check CI status using the commands from Section 4 above.

### Auto-Fix Loop Pattern

When asked to auto-fix CI, follow this loop:

1. Check CI status → identify failures
2. Read failure logs → understand the error
3. Use `read_file` + `patch`/`write_file` → fix the code
4. `git add . && git commit -m "fix: ..." && git push`
5. Wait for CI → re-check status
6. Repeat if still failing (up to 3 attempts, then ask the user)

## 6. Merging

**With gh:**

```bash
# Squash merge + delete branch (cleanest for feature branches)
gh pr merge --squash --delete-branch

# Enable auto-merge (merges when all checks pass)
gh pr merge --auto --squash --delete-branch
```

**With git + curl:**

```bash
PR_NUMBER=<number>

# Merge the PR via API (squash)
curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/merge \
  -d "{
    \"merge_method\": \"squash\",
    \"commit_title\": \"feat: add user authentication (#$PR_NUMBER)\"
  }"

# Delete the remote branch after merge
BRANCH=$(git branch --show-current)
git push origin --delete $BRANCH

# Switch back to main locally
git checkout main && git pull origin main
git branch -d $BRANCH
```

Merge methods: `"merge"` (merge commit), `"squash"`, `"rebase"`

### Enable Auto-Merge (curl)

```bash
# Auto-merge requires the repo to have it enabled in settings.
# This uses the GraphQL API since REST doesn't support auto-merge.
PR_NODE_ID=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['node_id'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/graphql \
  -d "{\"query\": \"mutation { enablePullRequestAutoMerge(input: {pullRequestId: \\\"$PR_NODE_ID\\\", mergeMethod: SQUASH}) { clientMutationId } }\"}"
```

## 7. Complete Workflow Example

```bash
# 1. Start from clean main
git checkout main && git pull origin main

# 2. Branch
git checkout -b fix/login-redirect-bug

# 3. (Agent makes code changes with file tools)

# 4. Commit
git add src/auth/login.py tests/test_login.py
git commit -m "fix: correct redirect URL after login

Preserves the ?next= parameter instead of always redirecting to /dashboard."

# 5. Push
git push -u origin HEAD

# 6. Create PR (picks gh or curl based on what's available)
# ... (see Section 3)

# 7. Monitor CI (see Section 4)

# 8. Merge when green (see Section 6)
```

## Useful PR Commands Reference

| Action | gh | git + curl |
|--------|-----|-----------|
| List my PRs | `gh pr list --author @me` | `curl -s -H "Authorization: token $GITHUB_TOKEN" "https://api.github.com/repos/$OWNER/$REPO/pulls?state=open"` |
| View PR diff | `gh pr diff` | `git diff main...HEAD` (local) or `curl -H "Accept: application/vnd.github.diff" ...` |
| Add comment | `gh pr comment N --body "..."` | `curl -X POST .../issues/N/comments -d '{"body":"..."}'` |
| Request review | `gh pr edit N --add-reviewer user` | `curl -X POST .../pulls/N/requested_reviewers -d '{"reviewers":["user"]}'` |
| Close PR | `gh pr close N` | `curl -X PATCH .../pulls/N -d '{"state":"closed"}'` |
| Check out someone's PR | `gh pr checkout N` | `git fetch origin pull/N/head:pr-N && git checkout pr-N` |
