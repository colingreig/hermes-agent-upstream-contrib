"""Default-ALLOW blocklist loader for autonomous Hermes work + publishing.

Reads ``references/blocklist.json`` (co-located with this module) and
exposes small, dependency-light lookup helpers. Anything not explicitly
listed is allowed by default — including future ClickUp projects and
publish domains. See ``references/blocklist.json`` for the policy
statement and how to request an addition (ClickUp task 86e29q8qz).

Import-safe module with no third-party dependencies — stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

_BLOCKLIST_PATH = Path(__file__).resolve().parent / "references" / "blocklist.json"

_EMPTY_BLOCKLIST: dict[str, list[str]] = {
    "clickup_project_blocklist": [],
    "publish_domain_blocklist": [],
}


def load_blocklist() -> dict[str, list[str]]:
    """Load the blocklist config from ``references/blocklist.json``.

    Falls back to an empty (fully-permissive) blocklist if the file is
    missing or malformed. A missing/corrupt config never fails closed into
    "block everything" — it fails open into "block nothing", matching the
    default-allow policy this file encodes.
    """
    try:
        raw = _BLOCKLIST_PATH.read_text(encoding="utf-8")
    except OSError:
        return {k: list(v) for k, v in _EMPTY_BLOCKLIST.items()}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {k: list(v) for k, v in _EMPTY_BLOCKLIST.items()}

    if not isinstance(data, dict):
        return {k: list(v) for k, v in _EMPTY_BLOCKLIST.items()}

    return {
        "clickup_project_blocklist": list(data.get("clickup_project_blocklist") or []),
        "publish_domain_blocklist": list(data.get("publish_domain_blocklist") or []),
    }


def is_project_blocked(project_name: str) -> bool:
    """Return True if ``project_name`` is on the ClickUp project blocklist.

    Case-insensitive exact-name membership check. Default-allow: any
    project not explicitly listed (including unknown/future projects)
    returns False.
    """
    if not project_name:
        return False
    blocked = {p.strip().lower() for p in load_blocklist()["clickup_project_blocklist"]}
    return project_name.strip().lower() in blocked


def _normalize_host(value: str) -> str:
    """Normalize a domain, host[:port], or full URL down to a bare hostname."""
    value = value.strip()
    if not value:
        return ""

    if "//" in value:
        host = urlsplit(value).netloc
    else:
        # Bare "example.com" or "example.com/path" or "example.com:443" —
        # give urlsplit a scheme-less-authority hint so it parses as a host.
        host = urlsplit(f"//{value}").netloc

    host = host.rsplit("@", 1)[-1]  # drop userinfo, if any
    host = host.split(":", 1)[0]  # drop port
    return host.strip(".").lower()


def is_publish_domain_blocked(domain: str) -> bool:
    """Return True if ``domain`` is blocked by the publish domain blocklist.

    Accepts a bare domain, a host with subdomain, or a full URL — it is
    normalized to a lowercase hostname first (scheme/path/port stripped).

    A blocklist entry containing a dot (e.g. ``"example.com"``) blocks that
    exact domain and any subdomain of it. A bare entry with no dot (e.g.
    ``"tofinoelopement"``) blocks any hostname where that token appears as
    a dot-separated label — i.e. it matches the domain regardless of TLD
    and regardless of subdomain (``tofinoelopement.com``,
    ``www.tofinoelopement.com``, ``tofinoelopement.co.uk``, ...).

    Default-allow: any domain not matched by a blocklist entry returns
    False, including unknown/future/owned domains.
    """
    host = _normalize_host(domain)
    if not host:
        return False

    labels = host.split(".")
    for raw_entry in load_blocklist()["publish_domain_blocklist"]:
        entry = raw_entry.strip().lower().strip(".")
        if not entry:
            continue
        if "." in entry:
            if host == entry or host.endswith("." + entry):
                return True
        else:
            if entry in labels:
                return True

    return False
