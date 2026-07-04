"""Tests for the unresolved-``${VAR}``-header diagnostic.

Context: when an MCP HTTP endpoint is split into multiple endpoints (e.g. a
combined ``/api/mcp`` becoming ``/api/mcp/cms`` + ``/api/mcp/agency-os``),
each split server config typically needs its own auth secret. It's easy to
add the new ``mcp_servers`` entry to config.yaml but forget to add its
matching secret to ``~/.hermes/.env``. ``_interpolate_env_vars`` silently
leaves the literal ``${VAR}`` placeholder in place when the var is unset, so
the server receives e.g. ``Authorization: Bearer ${MCP_AGENCY_OS_API_KEY}``
and rejects it with a bare 401 — with nothing in errors.log pointing at the
actual cause. ``_warn_unresolved_header_placeholders`` logs a clear,
actionable warning identifying the offending server/header/var so this stops
looking like a mystery repeating 401.
"""

from __future__ import annotations

import logging

from tools.mcp_tool import _warn_unresolved_header_placeholders


def test_logs_actionable_warning_for_unresolved_placeholder(caplog):
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        _warn_unresolved_header_placeholders(
            "agency-os",
            {"Authorization": "Bearer ${MCP_AGENCY_OS_API_KEY}"},
        )
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "agency-os" in msg
    assert "Authorization" in msg
    assert "MCP_AGENCY_OS_API_KEY" in msg
    assert "~/.hermes/.env" in msg


def test_supports_cursor_style_env_prefix(caplog):
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        _warn_unresolved_header_placeholders(
            "cms",
            {"Authorization": "Bearer ${env:MCP_CMS_API_KEY}"},
        )
    assert len(caplog.records) == 1
    assert "MCP_CMS_API_KEY" in caplog.records[0].getMessage()


def test_no_warning_when_fully_resolved(caplog):
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        _warn_unresolved_header_placeholders(
            "cms",
            {"Authorization": "Bearer sk-already-resolved-token"},
        )
    assert caplog.records == []


def test_no_warning_for_non_dict_headers(caplog):
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        _warn_unresolved_header_placeholders("cms", None)
        _warn_unresolved_header_placeholders("cms", "not-a-dict")
    assert caplog.records == []


def test_warns_once_per_unresolved_var_across_headers(caplog):
    with caplog.at_level(logging.WARNING, logger="tools.mcp_tool"):
        _warn_unresolved_header_placeholders(
            "agency-os",
            {
                "Authorization": "Bearer ${MCP_AGENCY_OS_API_KEY}",
                "X-Tenant": "${MCP_AGENCY_OS_TENANT}",
            },
        )
    assert len(caplog.records) == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("MCP_AGENCY_OS_API_KEY" in m for m in messages)
    assert any("MCP_AGENCY_OS_TENANT" in m for m in messages)
