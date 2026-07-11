"""Shared secret-shaped env-var *name* detection.

Both the execute_code sandbox (``tools/code_execution_tool.py::_scrub_child_env``)
and the terminal backend (``tools/environments/local.py``) need a
name-shape-based deny gate that catches credential-looking env vars
independent of whether the name happens to already be enumerated in either
module's static, provider/tool-registry-derived blocklist. Factored out into
this tiny, dependency-free module rather than having one module import the
other because:

- ``code_execution_tool.py`` registers itself into the global tool registry
  as a side effect of import (``registry.register(name="execute_code", ...)``
  at module scope). Importing it from ``tools/environments/local.py`` — which
  is itself imported very early by ``cli.py`` / ``gateway/run.py`` /
  ``cron/scheduler.py`` / ``tools/process_registry.py`` / etc. — would
  trigger tool registration as a side effect of merely loading the terminal
  backend, and risks subtle import-order bugs.
- ``tools/environments/local.py`` is imported by ``tools/browser_tool.py``,
  ``tools/computer_use/cua_backend.py``, and other low-level modules that
  ``code_execution_tool.py`` itself only imports lazily, inside a function
  (see its ``from tools.environments.base import touch_activity_if_due``) —
  a module-level import in the other direction would be a latent circular
  import waiting to happen.

Both call sites import the *patterns* from here instead.
"""

import re

# Secret-shaped substrings, checked case-insensitively against the *name* of
# an env var (never its value). This is the historical list from
# ``code_execution_tool.py``'s sandbox scrubber; kept here as the single
# source of truth so the two call sites can't drift.
#
# "PASS" is intentionally NOT included — it false-positives on legitimate
# non-secret vars (BYPASS_CACHE, COMPASS_DIR, PASSENGER_HOST) while
# PASSWORD/PASSWD already cover the credential cases.
SECRET_SUBSTRINGS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
    "PASSWD", "AUTH", "DSN", "WEBHOOK",
    # Abbreviations that appear in real-world credential variable names but
    # were previously undetected: CREDS (CREDENTIALS abbreviated), BEARER
    # (Authorization: Bearer tokens), APIKEY (written without an underscore).
    "CREDS", "BEARER", "APIKEY",
)

# Vendor-integration name *prefixes* that carry no SECRET_SUBSTRINGS match at
# all — so the substring check alone misses them — but are, in practice,
# always connection/credential material for a specific third-party system:
# WordPress (``WP_*``) and Microsoft Dynamics 365 (``D365*``) integration
# vars such as ``WP_FIELDSERVICESOFTWARE_IO`` or ``D365GROUP_DATABASE_URL``.
# Extend this tuple as new vendor-shaped leaks are discovered.
_DENIED_NAME_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"^WP_", r"^D365")
)


def has_secret_substring(name: str) -> bool:
    """True if *name* (case-insensitive) contains a secret-shaped substring."""
    upper = name.upper()
    return any(s in upper for s in SECRET_SUBSTRINGS)


def matches_denied_name_pattern(name: str) -> bool:
    """True if *name* matches a vendor-integration prefix that is always
    credential/connection material, even without a secret-shaped substring."""
    return any(p.match(name) for p in _DENIED_NAME_PATTERNS)


def is_secret_shaped_name(name: str) -> bool:
    """True if *name* looks like it carries a credential by either signal."""
    return has_secret_substring(name) or matches_denied_name_pattern(name)
