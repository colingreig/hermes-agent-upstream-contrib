"""Tests for hermes_blocklist module.

Default-ALLOW policy: only entries explicitly present in
references/blocklist.json are blocked. Everything else — including
future/unknown ClickUp projects and publish domains — is allowed.
"""

import json

import pytest

import hermes_blocklist
from hermes_blocklist import (
    is_project_blocked,
    is_publish_domain_blocked,
    load_blocklist,
)


class TestLoadBlocklist:
    def test_loads_real_config(self):
        data = load_blocklist()
        assert "oeconnection" in data["clickup_project_blocklist"]
        assert "partstech" in data["clickup_project_blocklist"]
        assert "tofinoelopement" in data["publish_domain_blocklist"]

    def test_missing_file_fails_open(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            hermes_blocklist, "_BLOCKLIST_PATH", tmp_path / "does-not-exist.json"
        )
        assert load_blocklist() == {
            "clickup_project_blocklist": [],
            "publish_domain_blocklist": [],
        }

    def test_malformed_json_fails_open(self, monkeypatch, tmp_path):
        bad_file = tmp_path / "blocklist.json"
        bad_file.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(hermes_blocklist, "_BLOCKLIST_PATH", bad_file)
        assert load_blocklist() == {
            "clickup_project_blocklist": [],
            "publish_domain_blocklist": [],
        }

    def test_non_dict_json_fails_open(self, monkeypatch, tmp_path):
        bad_file = tmp_path / "blocklist.json"
        bad_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        monkeypatch.setattr(hermes_blocklist, "_BLOCKLIST_PATH", bad_file)
        assert load_blocklist() == {
            "clickup_project_blocklist": [],
            "publish_domain_blocklist": [],
        }


class TestIsProjectBlocked:
    def test_default_allow_unlisted_project(self):
        assert is_project_blocked("some-brand-new-client") is False

    def test_oeconnection_blocked(self):
        assert is_project_blocked("oeconnection") is True

    def test_partstech_blocked(self):
        assert is_project_blocked("partstech") is True

    def test_case_insensitive(self):
        assert is_project_blocked("OEConnection") is True
        assert is_project_blocked("PartsTech") is True

    def test_empty_string_not_blocked(self):
        assert is_project_blocked("") is False


class TestIsPublishDomainBlocked:
    def test_default_allow_unlisted_domain(self):
        assert is_publish_domain_blocked("islandwellservice.ca") is False

    def test_bare_domain_blocked(self):
        assert is_publish_domain_blocked("tofinoelopement.com") is True

    def test_full_url_blocked(self):
        assert is_publish_domain_blocked("https://tofinoelopement.com/some/path") is True

    def test_subdomain_blocked(self):
        assert is_publish_domain_blocked("www.tofinoelopement.com") is True

    def test_http_scheme_blocked(self):
        assert is_publish_domain_blocked("http://tofinoelopement.com") is True

    def test_case_insensitive(self):
        assert is_publish_domain_blocked("HTTPS://WWW.TOFINOELOPEMENT.COM/x") is True

    def test_unrelated_owned_domain_allowed(self):
        assert is_publish_domain_blocked("islandwellservice.ca") is False
        assert is_publish_domain_blocked("https://www.islandwellservice.ca/booking") is False

    def test_empty_string_not_blocked(self):
        assert is_publish_domain_blocked("") is False

    def test_does_not_falsely_match_substring_lookalike(self):
        # "nottofinoelopement.com" contains the blocked token as a
        # substring but is a different domain — must not be blocked.
        assert is_publish_domain_blocked("nottofinoelopement.com") is False
