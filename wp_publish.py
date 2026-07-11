#!/usr/bin/env python3
"""Draft-first WordPress publish adapter (ClickUp 86e29q8ru).

Feeds the editorial QA gate: takes a content draft plus a target-site key,
resolves that site's WordPress Application Password credential, checks the
publish-domain blocklist, and creates a WP post with ``status: "draft"``
(or ``"pending"`` for a pending-review workflow) — **never** ``"publish"``.
Draft-first is non-negotiable; this module contains no code path that can
set ``status`` to ``"publish"``.

Ordering, by design:

1. Resolve the site key to a domain (``SITE_MAP``).
2. Consult ``hermes_blocklist.is_publish_domain_blocked`` for that domain
   **before anything else** — before credential resolution and before any
   network call. A blocked domain raises immediately and makes zero HTTP
   requests and resolves zero credentials.
3. Resolve the ``WP_<SITE>`` credential (lazy 1Password resolution first,
   falling back to a plain env var for local/dev overrides).
4. Convert the draft body to HTML if it looks like Markdown.
5. POST to ``{site_url}/wp-json/wp/v2/posts`` with ``status`` fixed to a
   draft-safe value.

Credentials are 1Password-backed env vars formatted ``username:app_password``
and are typically unset in a local shell (lazy per-task 1Password resolution,
see ``agent/lazy_secret_resolver.py``). This module never logs or prints a
resolved credential value.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

import requests

from hermes_blocklist import is_publish_domain_blocked

# Draft-only: any status not in this set is refused by publish_draft().
_ALLOWED_STATUSES = frozenset({"draft", "pending"})

# site-key (bare domain) -> env var name holding "username:app_password".
# Owned sites only. Unlisted/future owned domains are NOT auto-blocked by
# this map — is_publish_domain_blocked() is the single source of truth for
# refusal; an unknown site_key here just means "no credential configured
# yet" (a WPCredentialError, not a blocklist hit).
SITE_MAP: Dict[str, str] = {
    "excel.tv": "WP_EXCEL_TV",
    "academy.excel.tv": "WP_ACADEMY_EXCEL_TV",
    "fieldservicesoftware.io": "WP_FIELDSERVICESOFTWARE_IO",
    "hvacservicebellevue.com": "WP_HVACSERVICEBELLEVUE_COM",
    "islandwellservice.ca": "WP_ISLANDWELLSERVICE_CA",
    "jdmbuysell.com": "WP_JDMBUYSELL_COM",
}

_DEFAULT_TIMEOUT_SECONDS = 30


class WPPublishError(Exception):
    """Base class for all wp_publish errors."""


class WPUnknownSiteError(WPPublishError):
    """site_key is not in SITE_MAP."""


class WPBlockedDomainError(WPPublishError):
    """Target domain is on the publish-domain blocklist. No request was made."""


class WPCredentialError(WPPublishError):
    """The site's WP_<SITE> credential is unset, unresolvable, or malformed."""


class WPAPIError(WPPublishError):
    """The WordPress REST API returned an error or an unexpected response."""


@dataclass(frozen=True)
class SiteInfo:
    site_key: str
    domain: str
    site_url: str
    env_var: str


def resolve_site(site_key: str) -> SiteInfo:
    """Resolve a site key to its domain/site_url/credential-env-var.

    Raises WPUnknownSiteError with the list of valid keys if site_key isn't
    in SITE_MAP — this is a configuration error, distinct from a blocklist
    refusal.
    """
    if not site_key:
        raise WPUnknownSiteError("site_key must be a non-empty string")
    domain = site_key.strip().lower()
    env_var = SITE_MAP.get(domain)
    if env_var is None:
        valid = ", ".join(sorted(SITE_MAP))
        raise WPUnknownSiteError(
            f"Unknown site_key {site_key!r}. Known site keys: {valid}"
        )
    return SiteInfo(
        site_key=domain, domain=domain, site_url=f"https://{domain}", env_var=env_var
    )


def _decode_credential_value(value: str) -> Optional[str]:
    """Normalize a raw credential env var to a plain 'username:app_password' string.

    Two on-the-wire shapes are accepted, auto-detected:
      * plain ``username:app_password`` (as documented for this adapter), or
      * that same string pre-base64-encoded (some deployed 1Password items
        store the ready-to-use Basic-auth token directly). Observed in
        practice for the WP_<SITE> vars this adapter reads.

    Returns the decoded ``username:app_password`` string, or None if
    ``value`` matches neither shape.
    """
    if ":" in value:
        return value

    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except Exception:
        return None

    if ":" in decoded:
        return decoded
    return None


def resolve_credential(env_var: str) -> Tuple[str, str]:
    """Resolve WP_<SITE> to (username, app_password).

    Resolution order:
      1. ``agent.lazy_secret_resolver.get(env_var)`` — per-call lazy
         1Password resolution (the repo's standing pattern for on-demand
         secrets; see agent/lazy_secret_resolver.py). Import/lookup failures
         are swallowed here exactly like that module's own fail-open
         contract — they just mean "not resolvable this way, try the next".
      2. ``os.environ[env_var]`` — plain env var fallback for local/dev use
         or profiles that boot-export credentials.

    Accepts the resolved value as either plain ``username:app_password`` or
    that string pre-base64-encoded (see ``_decode_credential_value``).

    Never logs the resolved value. Raises WPCredentialError with an
    actionable (but secret-free) message if neither source yields a
    correctly-shaped credential.
    """
    import os

    value: Optional[str] = None

    try:
        from agent import lazy_secret_resolver

        value = lazy_secret_resolver.get(env_var)
    except Exception:
        value = None

    if not value:
        value = os.environ.get(env_var)

    if not value:
        raise WPCredentialError(
            f"Credential {env_var} is not set (checked lazy 1Password "
            "resolution and the environment). Configure the 1Password item "
            "backing this env var, or export it locally for testing."
        )

    decoded = _decode_credential_value(value)
    if decoded is None:
        raise WPCredentialError(
            f"Credential {env_var} is malformed: expected "
            "'username:app_password' (plain or base64-encoded)."
        )

    username, _, app_password = decoded.partition(":")
    username = username.strip()
    app_password = app_password.strip()
    if not username or not app_password:
        raise WPCredentialError(
            f"Credential {env_var} is malformed: expected "
            "'username:app_password' with both parts non-empty."
        )
    return username, app_password


def build_auth_header(username: str, app_password: str) -> str:
    """Build the `Authorization: Basic ...` header value. Never logged."""
    token = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode(
        "ascii"
    )
    return f"Basic {token}"


def _looks_like_html(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<") and ">" in stripped[:200]


def markdown_to_html(text: str) -> str:
    """Convert Markdown to HTML using the repo's existing `markdown` dependency."""
    import markdown as _markdown

    return _markdown.markdown(text, extensions=["extra", "sane_lists"])


def render_content_html(content: str, content_format: str = "auto") -> str:
    """Return HTML for the WP `content` field.

    content_format: "markdown" forces Markdown->HTML conversion, "html"
    passes content through unchanged, "auto" (default) sniffs the content
    and only converts if it doesn't already look like HTML.
    """
    if content_format == "html":
        return content
    if content_format == "markdown":
        return markdown_to_html(content)
    if content_format == "auto":
        return content if _looks_like_html(content) else markdown_to_html(content)
    raise ValueError(
        f"content_format must be one of 'markdown', 'html', 'auto', got {content_format!r}"
    )


def _build_payload(
    *,
    title: str,
    content_html: str,
    status: str,
    excerpt: Optional[str],
    categories: Optional[Iterable[int]],
    tags: Optional[Iterable[int]],
    featured_media: Optional[int],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "title": title,
        "content": content_html,
        "status": status,
    }
    if excerpt:
        payload["excerpt"] = excerpt
    if categories:
        payload["categories"] = list(categories)
    if tags:
        payload["tags"] = list(tags)
    if featured_media:
        payload["featured_media"] = featured_media
    return payload


def publish_draft(
    site_key: str,
    title: str,
    content: str,
    *,
    content_format: str = "auto",
    excerpt: Optional[str] = None,
    categories: Optional[Iterable[int]] = None,
    tags: Optional[Iterable[int]] = None,
    featured_media: Optional[int] = None,
    status: str = "draft",
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Create a draft (or pending) WP post for `site_key`. Never publishes.

    Refuses (WPBlockedDomainError, zero HTTP requests) if the site's domain
    is on the publish-domain blocklist. Refuses (WPCredentialError, zero
    HTTP requests) if the site's WP_<SITE> credential can't be resolved.
    Raises WPAPIError on network failure or a non-2xx/non-JSON response.

    Returns the parsed JSON response body (the created post) on success.
    """
    if status not in _ALLOWED_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_ALLOWED_STATUSES)}, got {status!r} "
            "— this adapter is draft-first and never sets status='publish'."
        )

    site = resolve_site(site_key)

    # Blocklist check happens BEFORE credential resolution and BEFORE any
    # network call — a blocked domain must short-circuit with zero requests
    # of any kind, HTTP or 1Password.
    if is_publish_domain_blocked(site.domain):
        raise WPBlockedDomainError(
            f"Refusing to publish to {site.domain!r}: domain is on the "
            "publish-domain blocklist (references/blocklist.json). No "
            "network request was made."
        )

    username, app_password = resolve_credential(site.env_var)
    content_html = render_content_html(content, content_format)
    payload = _build_payload(
        title=title,
        content_html=content_html,
        status=status,
        excerpt=excerpt,
        categories=categories,
        tags=tags,
        featured_media=featured_media,
    )

    headers = {
        "Authorization": build_auth_header(username, app_password),
        "Content-Type": "application/json",
    }
    url = f"{site.site_url}/wp-json/wp/v2/posts"

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise WPAPIError(f"Network error POSTing to {url}: {exc}") from exc

    if response.status_code >= 400:
        raise WPAPIError(
            f"WordPress REST API returned HTTP {response.status_code} for "
            f"{url}: {_safe_error_body(response)}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise WPAPIError(
            f"WordPress REST API returned a non-JSON response from {url}"
        ) from exc


def _safe_error_body(response: "requests.Response") -> str:
    """Best-effort, truncated error body for exception messages. No secrets in it."""
    try:
        body = response.text
    except Exception:
        return "<unreadable response body>"
    return body[:500]


# ---------------------------------------------------------------------------
# Thin CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a draft WordPress post from a Markdown/HTML file."
    )
    parser.add_argument(
        "--site", required=True, choices=sorted(SITE_MAP), help="Target site key (owned domain)."
    )
    parser.add_argument("--title", required=True, help="Post title.")
    parser.add_argument(
        "--content-file",
        required=True,
        help="Path to the draft body (Markdown or HTML). Use '-' for stdin.",
    )
    parser.add_argument(
        "--content-format",
        default="auto",
        choices=["auto", "markdown", "html"],
        help="Format of --content-file. Default: auto-detect.",
    )
    parser.add_argument("--excerpt", default=None)
    parser.add_argument(
        "--category", type=int, action="append", dest="categories", default=None
    )
    parser.add_argument("--tag", type=int, action="append", dest="tags", default=None)
    parser.add_argument("--featured-media", type=int, default=None)
    parser.add_argument(
        "--status",
        default="draft",
        choices=sorted(_ALLOWED_STATUSES),
        help="Draft-safe status only. 'publish' is not a valid choice.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)

    if args.content_file == "-":
        content = sys.stdin.read()
    else:
        with open(args.content_file, "r", encoding="utf-8") as f:
            content = f.read()

    try:
        result = publish_draft(
            args.site,
            args.title,
            content,
            content_format=args.content_format,
            excerpt=args.excerpt,
            categories=args.categories,
            tags=args.tags,
            featured_media=args.featured_media,
            status=args.status,
        )
    except WPPublishError as exc:
        print(f"wp_publish: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"id": result.get("id"), "status": result.get("status"), "link": result.get("link")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
