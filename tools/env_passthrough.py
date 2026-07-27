"""Environment variable passthrough registry.

Skills that declare ``required_environment_variables`` in their frontmatter
need those vars available in sandboxed execution environments (execute_code,
terminal).  By default both sandboxes strip secrets from the child process
environment for security.  This module provides a session-scoped allowlist
so skill-declared vars (and user-configured overrides) pass through.

Two sources feed the allowlist:

1. **Skill declarations** — when a skill is loaded via ``skill_view``, its
   ``required_environment_variables`` are registered here automatically.
2. **User config** — ``terminal.env_passthrough`` in config.yaml lets users
   explicitly allowlist vars for non-skill use cases.

Both ``code_execution_tool.py`` and ``tools/environments/local.py`` consult
:func:`is_env_passthrough` before stripping a variable.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Iterable, Mapping
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

# Session-scoped env names and child-only values.  Values are never copied into
# ``os.environ``: subprocess builders merge the overlay after sanitizing the
# inherited environment.  Both ContextVars use immutable tuple/frozenset
# snapshots so a copied context cannot mutate the object held by its parent or
# a sibling context.
_allowed_env_vars_var: ContextVar[frozenset[str]] = ContextVar(
    "_allowed_env_vars", default=frozenset()
)
_child_env_overlay_var: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "_child_env_overlay", default=()
)
_child_env_redaction_values_var: ContextVar[frozenset[str]] = ContextVar(
    "_child_env_redaction_values", default=frozenset()
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _get_allowed() -> frozenset[str]:
    """Return the env allowlist for the current context/session."""
    return _allowed_env_vars_var.get()


# Cache for the config-based allowlist, keyed on the config file's
# (mtime_ns, size) signature — same freshness strategy as
# ``hermes_cli.config.read_raw_config``. A bare "load once per process"
# cache made edits to ``terminal.env_passthrough`` in config.yaml a silent
# no-op until a full gateway restart; keying on the stat signature makes
# edits take effect on the next call instead.
#
# ``_config_passthrough`` holds the cached value (kept under this name for
# backward compatibility with external save/restore isolation patterns, e.g.
# ``tests/gateway/test_raft_adapter.py``). ``_config_passthrough_sig`` holds
# the (mtime_ns, size) signature the value was computed against.
_config_passthrough: frozenset[str] | None = None
_config_passthrough_sig: tuple[int, int] | None = None


def _is_hermes_provider_credential(name: str) -> bool:
    """True if ``name`` is a Hermes-managed provider credential (API key,
    token, or similar) per ``_HERMES_PROVIDER_ENV_BLOCKLIST``.

    Skill-declared ``required_environment_variables`` frontmatter must
    not be able to override this list — that was the bypass in
    GHSA-rhgp-j443-p4rf where a malicious skill registered
    ``ANTHROPIC_TOKEN`` / ``OPENAI_API_KEY`` as passthrough and received
    the credential in the ``execute_code`` child process, defeating the
    sandbox's scrubbing guarantee.

    Non-Hermes API keys (TENOR_API_KEY, NOTION_TOKEN, etc.) are NOT
    in the blocklist and remain legitimately registerable — skills that
    wrap third-party APIs still work.

    Fail closed: if the authoritative blocklist cannot be imported (partial
    install, import-time error, etc.) we treat the name as a protected
    provider credential and refuse passthrough, rather than fall open and
    let a skill tunnel a Hermes credential into the execute_code child.
    """
    try:
        from tools.environments.local import (
            _HERMES_PROVIDER_ENV_BLOCKLIST,
            _is_hermes_internal_secret,
        )
    except Exception as e:
        logger.warning(
            "env passthrough: provider credential blocklist import failed; "
            "failing closed and refusing passthrough registration for %r: %s",
            name,
            e,
        )
        return True
    # Dynamically-generated Hermes-internal secrets (AUXILIARY_*_API_KEY /
    # _BASE_URL side-LLM credentials, GATEWAY_RELAY_* relay-auth) are provider
    # credentials the static blocklist can't enumerate — they're injected per
    # task/relay at gateway startup. A skill must not be able to register them
    # as passthrough and tunnel them into an execute_code / terminal child.
    if _is_hermes_internal_secret(name):
        return True
    return name in _HERMES_PROVIDER_ENV_BLOCKLIST


def register_env_passthrough(var_names: Iterable[str]) -> None:
    """Register environment variable names as allowed in sandboxed environments.

    Typically called when a skill declares ``required_environment_variables``.

    Variables that are Hermes-managed provider credentials (from
    ``_HERMES_PROVIDER_ENV_BLOCKLIST``) are rejected here to preserve
    the ``execute_code`` sandbox's credential-scrubbing guarantee per
    GHSA-rhgp-j443-p4rf. A skill that needs to talk to a Hermes-managed
    provider should do so via the agent's main-process tools (web_search,
    web_extract, etc.) where the credential remains safely in the main
    process.

    Non-Hermes third-party API keys (TENOR_API_KEY, NOTION_TOKEN, etc.)
    pass through normally — they were never in the sandbox scrub list.
    """
    allowed = set(_get_allowed())
    for name in var_names:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or not _ENV_NAME_RE.fullmatch(name):
            continue
        if _is_hermes_provider_credential(name):
            logger.warning(
                "env passthrough: refusing to register Hermes provider "
                "credential %r (blocked by _HERMES_PROVIDER_ENV_BLOCKLIST). "
                "Skills must not override the execute_code sandbox's "
                "credential scrubbing; see GHSA-rhgp-j443-p4rf.",
                name,
            )
            continue
        allowed.add(name)
        logger.debug("env passthrough: registered %s", name)
    _allowed_env_vars_var.set(frozenset(allowed))


def register_child_env_overlay(
    values: Mapping[str, str],
    *,
    redact_values: bool = True,
) -> None:
    """Register context-local values to inject into child processes.

    This is deliberately stricter than passing an ``extra_env`` mapping at a
    call site: every name is first registered through the existing provider
    credential guard, and only accepted names are stored.  Callers therefore
    cannot use the value overlay to bypass the Hermes provider/internal-secret
    blocklists.  The overlay is consumed by the local foreground and
    background/PTY subprocess sanitizers and never mutates ``os.environ``.

    ``redact_values`` marks the values as literal secrets for terminal/process
    output redaction.  Non-secret routing values such as
    ``CLAUDE_PLUGIN_ROOT`` should pass ``False``.
    """
    if not isinstance(values, Mapping):
        return

    normalized: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            continue
        name = raw_name.strip()
        if not name or not _ENV_NAME_RE.fullmatch(name):
            continue
        normalized[name] = raw_value

    if not normalized:
        return

    register_env_passthrough(normalized)
    accepted = {
        name: value
        for name, value in normalized.items()
        if name in _get_allowed()
    }
    if not accepted:
        return

    overlay = dict(_child_env_overlay_var.get())
    overlay.update(accepted)
    _child_env_overlay_var.set(tuple(overlay.items()))

    if redact_values:
        redactions = set(_child_env_redaction_values_var.get())
        redactions.update(value for value in accepted.values() if value)
        _child_env_redaction_values_var.set(frozenset(redactions))


def get_child_env_overlay() -> dict[str, str]:
    """Return a copy of the current context's child-process value overlay."""
    return dict(_child_env_overlay_var.get())


def get_child_env_redaction_values() -> frozenset[str]:
    """Return literal child-only secret values that output must redact."""
    return _child_env_redaction_values_var.get()


def reset_env_passthrough_context() -> None:
    """Replace this context's skill/child environment state with an empty one.

    Unlike :func:`clear_env_passthrough`, this does not invalidate the
    process-wide config cache.  Prompt builders use it when beginning an
    independent job so sequential direct builds cannot inherit a prior job's
    plugin root or allowlist.  The immutable ContextVar snapshots ensure the
    reset affects only the current context, never concurrent sibling jobs.
    """
    _allowed_env_vars_var.set(frozenset())
    _child_env_overlay_var.set(())
    _child_env_redaction_values_var.set(frozenset())


def begin_env_passthrough_scope() -> tuple:
    """Start an empty child-env scope and return tokens for restoration.

    Cron calls this once per dispatched job.  Resetting the returned tokens
    restores the caller's prior state rather than globally clearing it, which
    is important when concurrent jobs run in copied contexts.
    """
    return (
        _allowed_env_vars_var.set(frozenset()),
        _child_env_overlay_var.set(()),
        _child_env_redaction_values_var.set(frozenset()),
    )


def reset_env_passthrough_scope(tokens: tuple) -> None:
    """Restore a scope created by :func:`begin_env_passthrough_scope`."""
    allowed_token, overlay_token, redaction_token = tokens
    _child_env_redaction_values_var.reset(redaction_token)
    _child_env_overlay_var.reset(overlay_token)
    _allowed_env_vars_var.reset(allowed_token)


def _load_config_passthrough() -> frozenset[str]:
    """Load ``tools.env_passthrough`` from config.yaml.

    Cached on the config file's (mtime_ns, size) signature — same freshness
    strategy as ``hermes_cli.config.read_raw_config`` — so edits to
    ``terminal.env_passthrough`` take effect on the next call instead of
    requiring a full process restart. Falls back to an uncached (and, on
    stat/read failure, empty) result if the config file's stat can't be
    determined.
    """
    global _config_passthrough, _config_passthrough_sig

    cache_key: tuple[int, int] | None
    try:
        from hermes_cli.config import get_config_path
        st = get_config_path().stat()
        cache_key = (st.st_mtime_ns, st.st_size)
    except Exception:
        cache_key = None

    if (
        cache_key is not None
        and _config_passthrough is not None
        and _config_passthrough_sig == cache_key
    ):
        return _config_passthrough

    result: set[str] = set()
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        passthrough = cfg_get(cfg, "terminal", "env_passthrough")
        if isinstance(passthrough, list):
            for item in passthrough:
                if not isinstance(item, str) or not item.strip():
                    continue
                name = item.strip()
                # Mirror the skill-path filter in register_env_passthrough:
                # Hermes-managed provider credentials must not be passed
                # through to execute_code / terminal children, regardless of
                # whether the request came from a skill or from config.yaml.
                # See GHSA-rhgp-j443-p4rf.
                if _is_hermes_provider_credential(name):
                    logger.warning(
                        "env passthrough: refusing to register Hermes "
                        "provider credential %r from config.yaml (blocked "
                        "by _HERMES_PROVIDER_ENV_BLOCKLIST). Operator "
                        "configuration must not override the execute_code "
                        "sandbox's credential scrubbing; see "
                        "GHSA-rhgp-j443-p4rf.",
                        name,
                    )
                    continue
                result.add(name)
    except Exception as e:
        logger.debug("Could not read tools.env_passthrough from config: %s", e)

    computed = frozenset(result)
    if cache_key is not None:
        _config_passthrough = computed
        _config_passthrough_sig = cache_key
    return computed


def is_env_passthrough(var_name: str) -> bool:
    """Check whether *var_name* is allowed to pass through to sandboxes.

    Returns ``True`` if the variable was registered by a skill or listed in
    the user's ``tools.env_passthrough`` config.
    """
    if var_name in _get_allowed():
        return True
    return var_name in _load_config_passthrough()


def get_all_passthrough() -> frozenset[str]:
    """Return the union of skill-registered and config-based passthrough vars."""
    return frozenset(_get_allowed()) | _load_config_passthrough()


def clear_env_passthrough() -> None:
    """Reset the skill-scoped allowlist (e.g. on session reset).

    Also drops the config-based allowlist cache so a manual clear forces
    a fresh read of ``terminal.env_passthrough`` from config.yaml on the
    next lookup, rather than serving a possibly-stale cached value.
    """
    global _config_passthrough, _config_passthrough_sig
    reset_env_passthrough_context()
    _config_passthrough = None
    _config_passthrough_sig = None
