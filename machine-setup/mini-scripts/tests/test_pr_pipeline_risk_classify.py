"""Tests for risk_classify.py — deterministic diff risk classifier.

Covers the ClickUp #287 regression: a pure-markdown/prose file quoting an
`Authorization: Bearer <token>` header or `api_key` in a curl example must
not classify HIGH just because the words pattern-match the auth/secrets
content rules. Real credential/auth code (by path or by content in a
non-doc file) must still classify HIGH.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
for path in (SCRIPTS, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import risk_classify  # noqa: E402


# --- inlined positive-control fixtures (mirrors .context/riskcal/fixtures/) --

FIXTURE_ENV_SECRET = """\
diff --git a/.env.production b/.env.production
index 1111111..2222222 100644
--- a/.env.production
+++ b/.env.production
@@ -3,3 +3,4 @@ NODE_ENV=production
 DATABASE_URL=postgres://user:pass@db.internal:5432/app
+STRIPE_API_KEY=FAKE_TEST_VALUE_NOT_A_REAL_SECRET_0123456789abcdef
"""

FIXTURE_BEARER_HEADER = """\
diff --git a/src/lib/apiClient.ts b/src/lib/apiClient.ts
index 3333333..4444444 100644
--- a/src/lib/apiClient.ts
+++ b/src/lib/apiClient.ts
@@ -10,6 +10,9 @@ export async function fetchUser(id: string) {
   const res = await fetch(`/api/users/${id}`, {
+    headers: {
+      Authorization: `Bearer ${session.accessToken}`,
+    },
   });
   return res.json();
 }
"""

FIXTURE_WORKFLOW_YAML = """\
diff --git a/.github/workflows/deploy.yml b/.github/workflows/deploy.yml
index 5555555..6666666 100644
--- a/.github/workflows/deploy.yml
+++ b/.github/workflows/deploy.yml
@@ -12,6 +12,8 @@ jobs:
   deploy:
     runs-on: ubuntu-latest
     steps:
+      - name: Deploy to production
+        run: wrangler deploy --env production
       - uses: actions/checkout@v4
"""

FIXTURE_SQL_MIGRATION = """\
diff --git a/migrations/0042_add_orders_table.sql b/migrations/0042_add_orders_table.sql
new file mode 100644
index 0000000..7777777
--- /dev/null
+++ b/migrations/0042_add_orders_table.sql
@@ -0,0 +1,7 @@
+CREATE TABLE orders (
+  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
+  customer_id uuid NOT NULL,
+  amount_cents integer NOT NULL,
+  created_at timestamptz NOT NULL DEFAULT now()
+);
+ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
"""

FIXTURE_STRIPE_AMOUNT = """\
diff --git a/src/billing/checkout.ts b/src/billing/checkout.ts
index 8888888..9999999 100644
--- a/src/billing/checkout.ts
+++ b/src/billing/checkout.ts
@@ -20,7 +20,7 @@ export async function createCharge(customerId: string) {
   return stripe.paymentIntents.create({
-    amount_cents: 4999,
+    amount_cents: 9999,
     currency: "usd",
     customer: customerId,
   });
"""

FIXTURE_AUTH_CHECK_REMOVED = """\
diff --git a/src/api/account/route.ts b/src/api/account/route.ts
index aaaaaaa..bbbbbbb 100644
--- a/src/api/account/route.ts
+++ b/src/api/account/route.ts
@@ -5,9 +5,7 @@ import { getSession } from "@/lib/auth";
 export async function GET(req: Request) {
-  const session = await getSession(req);
-  if (!session || !session.isSignedIn) {
-    return new Response("unauthorized", { status: 401 });
-  }
+  // TODO: re-enable auth check after debugging
+  const session = { isSignedIn: true, userId: "debug" };
   return Response.json({ account: session.userId });
 }
"""

POSITIVE_CONTROL_FIXTURES = {
    "a-env-secret": FIXTURE_ENV_SECRET,
    "b-bearer-header": FIXTURE_BEARER_HEADER,
    "c-workflow-yaml": FIXTURE_WORKFLOW_YAML,
    "d-sql-migration": FIXTURE_SQL_MIGRATION,
    "e-stripe-amount": FIXTURE_STRIPE_AMOUNT,
    "f-auth-check-removed": FIXTURE_AUTH_CHECK_REMOVED,
}

# --- #287 regression: markdown-only diff quoting auth-shaped prose ----------

MARKDOWN_AUTH_PROSE = """\
diff --git a/.claude/handoffs/2026-08-02-handoff.md b/.claude/handoffs/2026-08-02-handoff.md
new file mode 100644
index 000000000000..6e177d5c3aaf
--- /dev/null
+++ b/.claude/handoffs/2026-08-02-handoff.md
@@ -0,0 +1,4 @@
+# Handoff
+
+ClickUp API from this box:
+`curl -H "Authorization: Bearer $CLICKUP_API_TOKEN" https://api.clickup.com/api/v2/task/123`
"""
# Note: this fixture intentionally quotes ONLY the auth/session word
# ("Authorization: Bearer") and no secret-shaped word (e.g. "api_key") —
# per FIX 2, secrets/env content matches are NOT prose-suppressed, so a doc
# that also mentioned "api_key" would correctly classify HIGH.

# --- doc file changed alongside a real .env secret change -------------------

MARKDOWN_PLUS_ENV_SECRET = """\
diff --git a/README.md b/README.md
index 1234567..7654321 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
 # Project
+See Authorization: Bearer <token> example below.
diff --git a/.env.production b/.env.production
index 1111111..2222222 100644
--- a/.env.production
+++ b/.env.production
@@ -3,3 +3,4 @@ NODE_ENV=production
 DATABASE_URL=postgres://user:pass@db.internal:5432/app
+STRIPE_API_KEY=FAKE_TEST_VALUE_NOT_A_REAL_SECRET_0123456789abcdef
"""
# README.md line intentionally quotes ONLY the auth/session word
# ("Authorization: Bearer") — not a secrets/env word — so the fixture
# isolates the auth/session prose-suppression from the secrets/env rule,
# which stays fully live on doc paths per FIX 2.


# --- defect-1 regression: _DOC_PATH_RE narrowed to prose extensions only ----
# (previously matched any path under a docs/ directory, or any .txt file —
# so a real deploy script or secret note under docs/ or named *.txt would
# have been wrongly treated as a doc path.)

DOCS_DIR_SCRIPT = """\
diff --git a/docs/redeploy.sh b/docs/redeploy.sh
new file mode 100644
index 0000000..dead000
--- /dev/null
+++ b/docs/redeploy.sh
@@ -0,0 +1,2 @@
+#!/usr/bin/env bash
+gh workflow run deploy.yml
"""

TXT_SECRET = """\
diff --git a/notes.txt b/notes.txt
new file mode 100644
index 0000000..beef000
--- /dev/null
+++ b/notes.txt
@@ -0,0 +1,1 @@
+api_key = sk-live-xyz789abcdef
"""

# --- defect-2 regression: doc suppression narrowed to the auth/session rule
# only (previously applied to EVERY rule's content arm, so a real credential
# pasted into a .md file was wrongly waved through as low risk.)

MARKDOWN_REAL_SECRET = """\
diff --git a/TODO.md b/TODO.md
new file mode 100644
index 0000000..cafe0000
--- /dev/null
+++ b/TODO.md
@@ -0,0 +1,2 @@
+api_key = sk-live-AbC123DeF456
+password: hunter2correcthorse
"""

# Auth/session content IS still prose-suppressed by design (FIX 2 keeps that
# one rule opted in) — a Bearer token quoted in a markdown doc alone yields
# LOW, same as the PR #287 case. secrets/env (bare "password"/"api_key")
# would still catch a REAL credential in the same doc — see
# MARKDOWN_REAL_SECRET above, which has no bearer/authorization word and
# correctly classifies HIGH via secrets/env alone.
README_BEARER_TOKEN = """\
diff --git a/README.md b/README.md
new file mode 100644
index 0000000..1eaf0000
--- /dev/null
+++ b/README.md
@@ -0,0 +1,1 @@
+Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig
"""

DOCS_ARCHITECTURE_PROSE = """\
diff --git a/docs/architecture.md b/docs/architecture.md
new file mode 100644
index 0000000..f00d0000
--- /dev/null
+++ b/docs/architecture.md
@@ -0,0 +1,2 @@
+Example request:
+`curl -H "Authorization: Bearer $TOKEN" https://api.example.com/v1/widgets`
"""


class RiskClassifyFixtureTests(unittest.TestCase):
    def test_all_positive_control_fixtures_classify_high(self) -> None:
        for name, diff_text in POSITIVE_CONTROL_FIXTURES.items():
            with self.subTest(fixture=name):
                result = risk_classify.classify(diff_text)
                self.assertEqual(result["tier"], "high", f"{name} -> {result}")


class RiskClassifyDocExclusionTests(unittest.TestCase):
    def test_markdown_only_auth_prose_classifies_low(self) -> None:
        result = risk_classify.classify(MARKDOWN_AUTH_PROSE)
        self.assertEqual(result["tier"], "low", result)

    def test_real_env_secret_still_classifies_high(self) -> None:
        result = risk_classify.classify(FIXTURE_ENV_SECRET)
        self.assertEqual(result["tier"], "high", result)

    def test_markdown_alongside_real_env_secret_still_classifies_high(self) -> None:
        # Proves the doc exclusion is per-file, not per-diff: the .md hunk's
        # auth-shaped prose must not fire, but the .env secret hunk in the
        # SAME diff must still push the overall tier to high.
        result = risk_classify.classify(MARKDOWN_PLUS_ENV_SECRET)
        self.assertEqual(result["tier"], "high", result)
        labels = {s["label"] for s in result["surfaces"]}
        files = {s["file"] for s in result["surfaces"]}
        self.assertIn("secrets/env", labels)
        self.assertNotIn("README.md", files)

    def test_script_under_docs_dir_still_classifies_high(self) -> None:
        # Defect 1: a docs/ directory clause used to blanket-exempt any path
        # under docs/, including a real deploy script.
        result = risk_classify.classify(DOCS_DIR_SCRIPT)
        self.assertEqual(result["tier"], "high", result)

    def test_txt_file_with_secret_still_classifies_high(self) -> None:
        # Defect 1: .txt used to be a recognized "doc" extension.
        result = risk_classify.classify(TXT_SECRET)
        self.assertEqual(result["tier"], "high", result)

    def test_markdown_with_real_secret_classifies_high(self) -> None:
        # Defect 2: blanket per-rule suppression used to wave through a real
        # credential pasted into a .md file. secrets/env is not
        # prose_suppressible, so it must still fire on doc paths.
        result = risk_classify.classify(MARKDOWN_REAL_SECRET)
        self.assertEqual(result["tier"], "high", result)

    def test_markdown_bearer_token_alone_classifies_low(self) -> None:
        # Only the auth/session rule is prose_suppressible (by design, per
        # FIX 2 and the PR #287 rationale) — a Bearer token quoted in a doc
        # with no other secret-shaped word still classifies LOW. secrets/env
        # still guards real credential material (see
        # test_markdown_with_real_secret_classifies_high above).
        result = risk_classify.classify(README_BEARER_TOKEN)
        self.assertEqual(result["tier"], "low", result)

    def test_docs_architecture_auth_prose_classifies_low(self) -> None:
        # The PR #287 case itself: a curl example quoting "Authorization" in
        # a docs/*.md file must not classify HIGH.
        result = risk_classify.classify(DOCS_ARCHITECTURE_PROSE)
        self.assertEqual(result["tier"], "low", result)


if __name__ == "__main__":
    unittest.main()
