"""Tests for wp_publish (ClickUp 86e29q8ru).

Draft-first WordPress publish adapter. These tests are hermetic: the HTTP
layer (`requests.post`) is always monkeypatched, and no test ever depends
on real 1Password or WordPress credentials.

Key invariants under test:
  * A blocked publish domain (references/blocklist.json's
    publish_domain_blocklist, currently ["tofinoelopement"]) is refused
    BEFORE any HTTP request is made — zero calls to requests.post.
  * An allowed owned domain reaches the POST path.
  * The post status is always "draft" (or "pending") — never "publish".
  * A resolved credential is never written to logs/stdout/stderr.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import wp_publish  # noqa: E402
from wp_publish import (
    SITE_MAP,
    WPAPIError,
    WPBlockedDomainError,
    WPCredentialError,
    WPUnknownSiteError,
    build_auth_header,
    publish_draft,
    render_content_html,
    resolve_credential,
    resolve_site,
)


class _FakeResponse:
    def __init__(self, status_code=201, json_body=None, text_body=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text_body or str(self._json_body)

    def json(self):
        return self._json_body


@pytest.fixture(autouse=True)
def _no_lazy_secret_resolver(monkeypatch):
    """Force resolve_credential onto the plain-env-var fallback path.

    Avoids any dependency on agent.lazy_secret_resolver's manifest/SDK
    machinery in these tests — we only care that *some* value reaches
    resolve_credential, not which of the two lookup layers supplied it.
    """
    import agent.lazy_secret_resolver as lsr

    monkeypatch.setattr(lsr, "get", lambda name: None)


class TestResolveSite:
    def test_known_site_key(self):
        site = resolve_site("islandwellservice.ca")
        assert site.domain == "islandwellservice.ca"
        assert site.site_url == "https://islandwellservice.ca"
        assert site.env_var == "WP_ISLANDWELLSERVICE_CA"

    def test_all_documented_sites_present(self):
        assert SITE_MAP == {
            "excel.tv": "WP_EXCEL_TV",
            "academy.excel.tv": "WP_ACADEMY_EXCEL_TV",
            "fieldservicesoftware.io": "WP_FIELDSERVICESOFTWARE_IO",
            "hvacservicebellevue.com": "WP_HVACSERVICEBELLEVUE_COM",
            "islandwellservice.ca": "WP_ISLANDWELLSERVICE_CA",
            "jdmbuysell.com": "WP_JDMBUYSELL_COM",
        }

    def test_unknown_site_key_raises(self):
        with pytest.raises(WPUnknownSiteError):
            resolve_site("not-an-owned-site.example")

    def test_empty_site_key_raises(self):
        with pytest.raises(WPUnknownSiteError):
            resolve_site("")

    def test_case_insensitive(self):
        site = resolve_site("Excel.TV")
        assert site.domain == "excel.tv"


class TestResolveCredential:
    def test_resolves_from_env_fallback(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:abcd 1234 efgh")
        username, app_password = resolve_credential("WP_ISLANDWELLSERVICE_CA")
        assert username == "editor"
        assert app_password == "abcd 1234 efgh"

    def test_prefers_lazy_resolver_over_env(self, monkeypatch):
        import agent.lazy_secret_resolver as lsr

        monkeypatch.setattr(lsr, "get", lambda name: "lazy-user:lazy-pass")
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "env-user:env-pass")
        username, app_password = resolve_credential("WP_ISLANDWELLSERVICE_CA")
        assert (username, app_password) == ("lazy-user", "lazy-pass")

    def test_unset_credential_raises_actionable_error(self, monkeypatch):
        monkeypatch.delenv("WP_ISLANDWELLSERVICE_CA", raising=False)
        with pytest.raises(WPCredentialError, match="WP_ISLANDWELLSERVICE_CA"):
            resolve_credential("WP_ISLANDWELLSERVICE_CA")

    def test_malformed_credential_missing_colon_raises(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "no-colon-here")
        with pytest.raises(WPCredentialError, match="malformed"):
            resolve_credential("WP_ISLANDWELLSERVICE_CA")

    def test_malformed_credential_empty_parts_raises(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", ":app-password-only")
        with pytest.raises(WPCredentialError, match="malformed"):
            resolve_credential("WP_ISLANDWELLSERVICE_CA")

    def test_resolves_from_base64_encoded_env_value(self, monkeypatch):
        import base64

        raw = base64.b64encode(b"editor:abcd1234efgh").decode("ascii")
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", raw)
        username, app_password = resolve_credential("WP_ISLANDWELLSERVICE_CA")
        assert (username, app_password) == ("editor", "abcd1234efgh")

    def test_base64_value_with_no_colon_after_decode_raises(self, monkeypatch):
        import base64

        raw = base64.b64encode(b"nocolonhere").decode("ascii")
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", raw)
        with pytest.raises(WPCredentialError, match="malformed"):
            resolve_credential("WP_ISLANDWELLSERVICE_CA")


class TestBuildAuthHeader:
    def test_matches_documented_basic_auth_scheme(self):
        import base64

        header = build_auth_header("editor", "app-pass-123")
        expected = "Basic " + base64.b64encode(b"editor:app-pass-123").decode("ascii")
        assert header == expected


class TestRenderContentHtml:
    def test_markdown_converted_to_html(self):
        html = render_content_html("# Title\n\nSome **bold** text.", "markdown")
        assert "<h1>" in html
        assert "<strong>bold</strong>" in html

    def test_html_passed_through_unchanged(self):
        raw = "<p>Already HTML</p>"
        assert render_content_html(raw, "html") == raw

    def test_auto_detects_markdown(self):
        html = render_content_html("## Heading\n\nplain text", "auto")
        assert "<h2>" in html

    def test_auto_detects_html(self):
        raw = "<p>Already HTML</p>"
        assert render_content_html(raw, "auto") == raw

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            render_content_html("text", "yaml")


class TestPublishDraftBlocklist:
    """The core safety property: a blocked domain makes ZERO HTTP requests."""

    def test_blocked_domain_zero_http_calls_bare_domain(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
        # tofinoelopement isn't in SITE_MAP, so add a temp owned-style entry
        # pointing at the blocked token to exercise the real blocklist path.
        monkeypatch.setitem(SITE_MAP, "tofinoelopement.com", "WP_TOFINOELOPEMENT_COM")
        monkeypatch.setenv("WP_TOFINOELOPEMENT_COM", "user:pass")

        with pytest.raises(WPBlockedDomainError, match="tofinoelopement.com"):
            publish_draft("tofinoelopement.com", "Title", "Body")

        assert calls == []

    def test_blocked_domain_zero_http_calls_full_url_style_key(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setitem(SITE_MAP, "www.tofinoelopement.com", "WP_TOFINO_WWW")
        monkeypatch.setenv("WP_TOFINO_WWW", "user:pass")

        with pytest.raises(WPBlockedDomainError):
            publish_draft("www.tofinoelopement.com", "Title", "Body")

        assert calls == []

    def test_blocked_domain_no_credential_needed(self, monkeypatch):
        """Refusal happens even with NO credential configured at all —
        proves the blocklist check runs strictly before credential
        resolution, not just before the network call."""
        calls = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setitem(SITE_MAP, "tofinoelopement.net", "WP_TOFINO_NET")
        monkeypatch.delenv("WP_TOFINO_NET", raising=False)

        with pytest.raises(WPBlockedDomainError):
            publish_draft("tofinoelopement.net", "Title", "Body")

        assert calls == []

    def test_blocklist_consulted_via_real_loader(self, monkeypatch):
        """Sanity check that publish_draft is actually wired to
        hermes_blocklist.is_publish_domain_blocked and not a local copy."""
        called_with = []
        real = wp_publish.is_publish_domain_blocked

        def spy(domain):
            called_with.append(domain)
            return real(domain)

        monkeypatch.setattr(wp_publish, "is_publish_domain_blocked", spy)
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "user:pass")
        monkeypatch.setattr(
            "requests.post", lambda *a, **kw: _FakeResponse(201, {"id": 1, "status": "draft"})
        )

        publish_draft("islandwellservice.ca", "Title", "Body")
        assert called_with == ["islandwellservice.ca"]


class TestPublishDraftAllowedDomain:
    def test_allowed_domain_reaches_post_path(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:app-secret-pw")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _FakeResponse(201, {"id": 42, "status": "draft", "link": "https://islandwellservice.ca/?p=42"})

        monkeypatch.setattr("requests.post", fake_post)

        result = publish_draft(
            "islandwellservice.ca",
            "My Post",
            "# Hello\n\nWorld",
            excerpt="A short excerpt",
            categories=[1],
            tags=[2, 3],
        )

        assert result["id"] == 42
        assert captured["url"] == "https://islandwellservice.ca/wp-json/wp/v2/posts"
        assert captured["json"]["status"] == "draft"
        assert captured["json"]["title"] == "My Post"
        assert "<h1>Hello</h1>" in captured["json"]["content"]
        assert captured["json"]["excerpt"] == "A short excerpt"
        assert captured["json"]["categories"] == [1]
        assert captured["json"]["tags"] == [2, 3]
        assert captured["headers"]["Authorization"].startswith("Basic ")

    def test_status_always_draft_by_default(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:app-secret-pw")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeResponse(201, {"id": 1, "status": "draft"})

        monkeypatch.setattr("requests.post", fake_post)
        publish_draft("islandwellservice.ca", "T", "B")
        assert captured["json"]["status"] == "draft"

    def test_pending_status_allowed(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:app-secret-pw")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeResponse(201, {"id": 1, "status": "pending"})

        monkeypatch.setattr("requests.post", fake_post)
        publish_draft("islandwellservice.ca", "T", "B", status="pending")
        assert captured["json"]["status"] == "pending"

    @pytest.mark.parametrize("bad_status", ["publish", "future", "private", ""])
    def test_publish_status_rejected_before_any_network_call(self, monkeypatch, bad_status):
        calls = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:app-secret-pw")

        with pytest.raises(ValueError):
            publish_draft("islandwellservice.ca", "T", "B", status=bad_status)

        assert calls == []

    def test_missing_credential_raises_before_network_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
        monkeypatch.delenv("WP_ISLANDWELLSERVICE_CA", raising=False)

        with pytest.raises(WPCredentialError):
            publish_draft("islandwellservice.ca", "T", "B")

        assert calls == []

    def test_http_error_response_raises_wpapierror(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:app-secret-pw")
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **kw: _FakeResponse(401, text_body="Unauthorized"),
        )
        with pytest.raises(WPAPIError, match="401"):
            publish_draft("islandwellservice.ca", "T", "B")

    def test_network_exception_raises_wpapierror(self, monkeypatch):
        import requests as real_requests

        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:app-secret-pw")

        def raise_conn_error(*a, **kw):
            raise real_requests.ConnectionError("boom")

        monkeypatch.setattr("requests.post", raise_conn_error)
        with pytest.raises(WPAPIError, match="Network error"):
            publish_draft("islandwellservice.ca", "T", "B")

    def test_unknown_site_key_raises_before_network_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.post", lambda *a, **kw: calls.append((a, kw)))
        with pytest.raises(WPUnknownSiteError):
            publish_draft("not-owned.example", "T", "B")
        assert calls == []


class TestSecretsNeverLogged:
    def test_credential_value_never_in_exception_messages(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:super-secret-app-pw")
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **kw: _FakeResponse(500, text_body="Internal Server Error"),
        )
        with pytest.raises(WPAPIError) as excinfo:
            publish_draft("islandwellservice.ca", "T", "B")
        assert "super-secret-app-pw" not in str(excinfo.value)

    def test_credential_value_never_written_via_logging(self, monkeypatch, caplog):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:super-secret-app-pw")
        monkeypatch.setattr(
            "requests.post",
            lambda *a, **kw: _FakeResponse(201, {"id": 1, "status": "draft"}),
        )
        with caplog.at_level(logging.DEBUG):
            publish_draft("islandwellservice.ca", "T", "B")
        assert "super-secret-app-pw" not in caplog.text

    def test_auth_header_is_base64_not_plaintext_credential(self, monkeypatch):
        monkeypatch.setenv("WP_ISLANDWELLSERVICE_CA", "editor:super-secret-app-pw")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return _FakeResponse(201, {"id": 1, "status": "draft"})

        monkeypatch.setattr("requests.post", fake_post)
        publish_draft("islandwellservice.ca", "T", "B")
        assert "super-secret-app-pw" not in captured["headers"]["Authorization"]
