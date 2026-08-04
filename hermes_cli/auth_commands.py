"""Credential-pool auth subcommands."""

from __future__ import annotations

import math
import sys
import time
from types import SimpleNamespace
import uuid

from agent.credential_pool import (
    AUTH_TYPE_API_KEY,
    AUTH_TYPE_OAUTH,
    CUSTOM_POOL_PREFIX,
    SOURCE_MANUAL,
    SOURCE_MANUAL_DEVICE_CODE,
    STATUS_EXHAUSTED,
    STRATEGY_FILL_FIRST,
    STRATEGY_ROUND_ROBIN,
    STRATEGY_RANDOM,
    STRATEGY_LEAST_USED,
    PooledCredential,
    _exhausted_until,
    _normalize_custom_pool_name,
    get_pool_strategy,
    label_from_token,
    list_custom_pool_providers,
    load_pool,
)
import hermes_cli.auth as auth_mod
from hermes_cli.auth import PROVIDER_REGISTRY
from hermes_constants import OPENROUTER_BASE_URL
from hermes_cli.secret_prompt import masked_secret_prompt


# Providers that support OAuth login in addition to API keys.
_OAUTH_CAPABLE_PROVIDERS = {"anthropic", "nous", "openai-codex", "xai-oauth", "qwen-oauth", "minimax-oauth"}


def _get_custom_provider_names() -> list:
    """Return list of (display_name, pool_key, provider_key) tuples."""
    try:
        from hermes_cli.config import get_compatible_custom_providers, load_config

        config = load_config()
    except Exception:
        return []
    result = []
    for entry in get_compatible_custom_providers(config):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        pool_key = f"{CUSTOM_POOL_PREFIX}{_normalize_custom_pool_name(name)}"
        provider_key = str(entry.get("provider_key", "") or "").strip()
        result.append((name.strip(), pool_key, provider_key))
    return result


def _resolve_custom_provider_input(raw: str) -> str | None:
    """If raw input matches a custom_providers entry name (case-insensitive), return its pool key."""
    normalized = (raw or "").strip().lower().replace(" ", "-")
    if not normalized:
        return None
    # Direct match on 'custom:name' format
    if normalized.startswith(CUSTOM_POOL_PREFIX):
        return normalized
    for display_name, pool_key, provider_key in _get_custom_provider_names():
        if _normalize_custom_pool_name(display_name) == normalized:
            return pool_key
        if provider_key and provider_key.strip().lower() == normalized:
            return pool_key
    return None


def _normalize_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized in {"or", "open-router"}:
        return "openrouter"
    if normalized in {"grok-oauth", "xai-oauth", "x-ai-oauth", "xai-grok-oauth"}:
        return "xai-oauth"
    # Check if it matches a custom provider name
    custom_key = _resolve_custom_provider_input(normalized)
    if custom_key:
        return custom_key
    return normalized


def _provider_base_url(provider: str) -> str:
    if provider == "openrouter":
        return OPENROUTER_BASE_URL
    if provider.startswith(CUSTOM_POOL_PREFIX):
        from agent.credential_pool import _get_custom_provider_config

        cp_config = _get_custom_provider_config(provider)
        if cp_config:
            return str(cp_config.get("base_url") or "").strip()
        return ""
    pconfig = PROVIDER_REGISTRY.get(provider)
    return pconfig.inference_base_url if pconfig else ""


def _oauth_default_label(provider: str, count: int) -> str:
    return f"{provider}-oauth-{count}"


def _api_key_default_label(count: int) -> str:
    return f"api-key-{count}"


def _display_source(source: str) -> str:
    return source.split(":", 1)[1] if source.startswith("manual:") else source


def _classify_exhausted_status(entry) -> tuple[str, bool]:
    code = getattr(entry, "last_error_code", None)
    reason = str(getattr(entry, "last_error_reason", "") or "").strip().lower()
    message = str(getattr(entry, "last_error_message", "") or "").strip().lower()

    if code == 429 or any(token in reason for token in ("rate_limit", "usage_limit", "quota", "exhausted")) or any(
        token in message for token in ("rate limit", "usage limit", "quota", "too many requests")
    ):
        return "rate-limited", True

    if code in {401, 403} or any(token in reason for token in ("invalid_token", "invalid_grant", "unauthorized", "forbidden", "auth")) or any(
        token in message for token in ("unauthorized", "forbidden", "expired", "revoked", "invalid token", "authentication")
    ):
        return "auth failed", False

    return "exhausted", True



def _format_exhausted_status(entry) -> str:
    if entry.last_status != STATUS_EXHAUSTED:
        return ""
    label, show_retry_window = _classify_exhausted_status(entry)
    reason = getattr(entry, "last_error_reason", None)
    reason_text = f" {reason}" if isinstance(reason, str) and reason.strip() else ""
    code = f" ({entry.last_error_code})" if entry.last_error_code else ""
    if not show_retry_window:
        return f" {label}{reason_text}{code} (re-auth may be required)"
    exhausted_until = _exhausted_until(entry)
    if exhausted_until is None:
        return f" {label}{reason_text}{code}"
    remaining = max(0, int(math.ceil(exhausted_until - time.time())))
    if remaining <= 0:
        return f" {label}{reason_text}{code} (ready to retry)"
    minutes, seconds = divmod(remaining, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        wait = f"{days}d {hours}h"
    elif hours:
        wait = f"{hours}h {minutes}m"
    elif minutes:
        wait = f"{minutes}m {seconds}s"
    else:
        wait = f"{seconds}s"
    return f" {label}{reason_text}{code} ({wait} left)"


def _format_taxonomy_suffix(entry) -> str:
    """Render the provider-failure taxonomy fields (86e2mb8nv/86e2mb8p0/
    86e2mb8p5) a pool entry may carry: ``last_failure_kind`` (the deeper
    classification beyond ``last_status``, e.g. ``auth_permanent`` vs a
    plain ``exhausted``), the out-of-band probe verdict that corroborated
    or denied it, and remaining usage headroom when a provider exposes one.

    Every field is optional (most entries — and every ``PooledCredential``
    written before 86e2mb8nv landed — carry none of them), so this returns
    ``""`` unless at least one piece has a value; callers append the result
    directly to an existing status line without needing to know which
    fields, if any, were populated.
    """
    parts = []
    kind = getattr(entry, "last_failure_kind", None)
    if isinstance(kind, str) and kind.strip():
        parts.append(f"kind={kind.strip()}")
    verdict = getattr(entry, "last_probe_verdict", None)
    if isinstance(verdict, str) and verdict.strip():
        parts.append(f"probe={verdict.strip()}")
    usage_percent = getattr(entry, "usage_percent", None)
    if isinstance(usage_percent, (int, float)):
        headroom = max(0.0, min(100.0, 100.0 - float(usage_percent)))
        parts.append(f"headroom={headroom:.0f}%")
    if not parts:
        return ""
    return f" [{' '.join(parts)}]"


def auth_add_command(args) -> None:
    provider = _normalize_provider(getattr(args, "provider", ""))
    if provider not in PROVIDER_REGISTRY and provider != "openrouter" and not provider.startswith(CUSTOM_POOL_PREFIX):
        raise SystemExit(f"Unknown provider: {provider}")

    requested_type = str(getattr(args, "auth_type", "") or "").strip().lower()
    if requested_type in {AUTH_TYPE_API_KEY, "api-key"}:
        requested_type = AUTH_TYPE_API_KEY
    if not requested_type:
        if provider.startswith(CUSTOM_POOL_PREFIX):
            requested_type = AUTH_TYPE_API_KEY
        else:
            requested_type = AUTH_TYPE_OAUTH if provider in _OAUTH_CAPABLE_PROVIDERS else AUTH_TYPE_API_KEY

    pool = load_pool(provider)

    # Clear ALL suppressions for this provider — re-adding a credential is
    # a strong signal the user wants auth re-enabled.  This covers env:*
    # (shell-exported vars), gh_cli (copilot), claude_code, qwen-cli,
    # device_code (codex), etc.  One consistent re-engagement pattern.
    # Matches the Codex device_code re-link pattern that predates this.
    if not provider.startswith(CUSTOM_POOL_PREFIX):
        try:
            from hermes_cli.auth import (
                _load_auth_store,
                unsuppress_credential_source,
            )
            suppressed = _load_auth_store().get("suppressed_sources", {})
            for src in list(suppressed.get(provider, []) or []):
                unsuppress_credential_source(provider, src)
        except Exception:
            pass

    if requested_type == AUTH_TYPE_API_KEY:
        token = (getattr(args, "api_key", None) or "").strip()
        if not token:
            token = masked_secret_prompt("Paste your API key: ").strip()
        if not token:
            raise SystemExit("No API key provided.")
        default_label = _api_key_default_label(len(pool.entries()) + 1)
        label = (getattr(args, "label", None) or "").strip()
        if not label:
            if sys.stdin.isatty():
                label = input(f"Label (optional, default: {default_label}): ").strip() or default_label
            else:
                label = default_label
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source=SOURCE_MANUAL,
            access_token=token,
            base_url=_provider_base_url(provider),
        )
        pool.add_entry(entry)
        print(f'Added {provider} credential #{len(pool.entries())}: "{label}"')
        return

    if provider == "anthropic":
        from agent import anthropic_adapter as anthropic_mod

        creds = anthropic_mod.run_hermes_oauth_login_pure()
        if not creds:
            raise SystemExit("Anthropic OAuth login did not return credentials.")
        label = (getattr(args, "label", None) or "").strip() or label_from_token(
            creds["access_token"],
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:hermes_pkce",
            access_token=creds["access_token"],
            refresh_token=creds.get("refresh_token"),
            expires_at_ms=creds.get("expires_at_ms"),
            base_url=_provider_base_url(provider),
        )
        pool.add_entry(entry)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    if provider == "nous":
        # Codex-style auto-import: if a shared Nous credential lives at
        # <hermes-root>/shared/nous_auth.json (written by any previous
        # successful login), offer to import it instead of running the
        # full device-code flow. This makes `hermes --profile <name>
        # auth add nous --type oauth` a one-tap operation for users who
        # run multiple profiles.
        shared = auth_mod._read_shared_nous_state()
        if shared:
            try:
                path = auth_mod._nous_shared_store_path()
            except RuntimeError:
                path = None
            print()
            if path:
                print(f"Found existing Nous OAuth credentials at {path}")
            else:
                print("Found existing shared Nous OAuth credentials")
            try:
                do_import = input("Import these credentials? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                do_import = "y"
            if do_import in {"", "y", "yes"}:
                print("Rehydrating Nous session from shared credentials...")
                rehydrated = auth_mod._try_import_shared_nous_state(
                    timeout_seconds=getattr(args, "timeout", None) or 15.0,
                )
                if rehydrated is not None:
                    custom_label = (getattr(args, "label", None) or "").strip() or None
                    entry = auth_mod.persist_nous_credentials(rehydrated, label=custom_label)
                    shown_label = entry.label if entry is not None else label_from_token(
                        rehydrated.get("access_token", ""), _oauth_default_label(provider, 1),
                    )
                    print(f'Imported {provider} OAuth credentials: "{shown_label}"')
                    return
                # Rehydrate failed (expired refresh_token, portal down, etc.)
                # — fall through to device-code flow.
                print("Could not refresh shared credentials — falling back to device-code login.")

        creds = auth_mod._nous_device_code_login(
            portal_base_url=getattr(args, "portal_url", None),
            inference_base_url=getattr(args, "inference_url", None),
            client_id=getattr(args, "client_id", None),
            scope=getattr(args, "scope", None),
            open_browser=not getattr(args, "no_browser", False),
            timeout_seconds=getattr(args, "timeout", None) or 15.0,
            insecure=bool(getattr(args, "insecure", False)),
            ca_bundle=getattr(args, "ca_bundle", None),
        )
        # Honor `--label <name>` so nous matches other providers' UX.  The
        # helper embeds this into providers.nous so that label_from_token
        # doesn't overwrite it on every subsequent load_pool("nous").
        custom_label = (getattr(args, "label", None) or "").strip() or None
        entry = auth_mod.persist_nous_credentials(creds, label=custom_label)
        shown_label = entry.label if entry is not None else label_from_token(
            creds.get("access_token", ""), _oauth_default_label(provider, 1),
        )
        print(f'Saved {provider} OAuth device-code credentials: "{shown_label}"')
        return

    if provider == "openai-codex":
        creds = auth_mod._codex_device_code_login()
        label = (getattr(args, "label", None) or "").strip() or label_from_token(
            creds["tokens"]["access_token"],
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        # Add a distinct, self-contained pool entry per account (matching the
        # qwen-oauth / minimax-oauth multi-account patterns, and the
        # xai-oauth path below) instead of routing through the singleton
        # ``_save_codex_tokens`` save path.
        # The singleton round-trip collapsed every added account into the
        # latest login: a second ``hermes auth add openai-codex`` overwrote
        # the first account's singleton-mirrored ``device_code`` entry rather
        # than creating an independent one (#39236). ``manual:device_code``
        # entries refresh from their own token pair, so they need no singleton
        # shadow.
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=SOURCE_MANUAL_DEVICE_CODE,
            access_token=creds["tokens"]["access_token"],
            refresh_token=creds["tokens"].get("refresh_token"),
            base_url=creds.get("base_url"),
            last_refresh=creds.get("last_refresh"),
        )
        first_credential = not pool.entries()
        pool.add_entry(entry)
        # Adding the first Codex credential should make it the active provider
        # (the old singleton save path did this implicitly via
        # _save_provider_state). Subsequent adds leave the active provider as-is.
        if first_credential:
            auth_mod.mark_provider_active_if_unset(provider)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    if provider == "xai-oauth":
        creds = auth_mod._xai_oauth_device_code_login(
            timeout_seconds=getattr(args, "timeout", None) or 20.0,
            open_browser=not getattr(args, "no_browser", False),
        )
        label = (getattr(args, "label", None) or "").strip() or label_from_token(
            creds["tokens"]["access_token"],
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        # Add a distinct, self-contained pool entry per account (matching the
        # openai-codex / qwen-oauth / minimax-oauth patterns) instead of
        # routing through the singleton ``_save_xai_oauth_tokens`` save path.
        # The singleton round-trip collapsed every added account into the
        # latest login: a second ``hermes auth add xai-oauth`` overwrote the
        # first account's singleton-mirrored ``device_code`` entry rather than
        # creating an independent one. ``manual:device_code`` entries refresh
        # from their own token pair (``_sync_xai_oauth_entry_from_auth_store``
        # only adopts the singleton for ``source=="device_code"``), so they
        # need no singleton shadow.
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=SOURCE_MANUAL_DEVICE_CODE,
            access_token=creds["tokens"]["access_token"],
            refresh_token=creds["tokens"].get("refresh_token"),
            base_url=creds.get("base_url") or auth_mod.DEFAULT_XAI_OAUTH_BASE_URL,
            last_refresh=creds.get("last_refresh"),
        )
        first_credential = not pool.entries()
        pool.add_entry(entry)
        # Adding the first xAI credential should make it the active provider
        # (the old singleton save path did this implicitly via
        # _save_provider_state). Subsequent adds leave the active provider as-is.
        if first_credential:
            auth_mod.mark_provider_active_if_unset(provider)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    if provider == "qwen-oauth":
        creds = auth_mod.resolve_qwen_runtime_credentials(refresh_if_expiring=False)
        auth_mod._mark_qwen_oauth_active(creds)
        label = (getattr(args, "label", None) or "").strip() or label_from_token(
            creds["api_key"],
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:qwen_cli",
            access_token=creds["api_key"],
            base_url=creds.get("base_url"),
        )
        pool.add_entry(entry)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    if provider == "minimax-oauth":
        creds = auth_mod._minimax_oauth_login(
            open_browser=not getattr(args, "no_browser", False),
            timeout_seconds=getattr(args, "timeout", None) or 15.0,
        )
        label = (getattr(args, "label", None) or "").strip() or label_from_token(
            creds["access_token"],
            _oauth_default_label(provider, len(pool.entries()) + 1),
        )
        entry = PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:minimax_oauth",
            access_token=creds["access_token"],
            refresh_token=creds.get("refresh_token"),
            base_url=creds.get("inference_base_url"),
        )
        pool.add_entry(entry)
        print(f'Added {provider} OAuth credential #{len(pool.entries())}: "{entry.label}"')
        return

    raise SystemExit(f"`hermes auth add {provider}` is not implemented for auth type {requested_type} yet.")


def auth_list_command(args) -> None:
    provider_filter = _normalize_provider(getattr(args, "provider", "") or "")
    if provider_filter:
        providers = [provider_filter]
    else:
        providers = sorted({
            *PROVIDER_REGISTRY.keys(),
            "openrouter",
            *list_custom_pool_providers(),
        })
    for provider in providers:
        pool = load_pool(provider)
        entries = pool.entries()
        if not entries:
            continue
        current = pool.peek()
        print(f"{provider} ({len(entries)} credentials):")
        for idx, entry in enumerate(entries, start=1):
            marker = "  "
            if current is not None and entry.id == current.id:
                marker = "← "
            status = _format_exhausted_status(entry)
            taxonomy = _format_taxonomy_suffix(entry)
            source = _display_source(entry.source)
            print(f"  #{idx}  {entry.label:<20} {entry.auth_type:<7} {source}{status}{taxonomy} {marker}".rstrip())
        print()


def auth_remove_command(args) -> None:
    provider = _normalize_provider(getattr(args, "provider", ""))
    target = getattr(args, "target", None)
    if target is None:
        target = getattr(args, "index", None)
    pool = load_pool(provider)
    index, matched, error = pool.resolve_target(target)
    if matched is None or index is None:
        raise SystemExit(f"{error} Provider: {provider}.")
    removed = pool.remove_index(index)
    if removed is None:
        raise SystemExit(f'No credential matching "{target}" for provider {provider}.')
    print(f"Removed {provider} credential #{index} ({removed.label})")

    # Unified removal dispatch.  Every credential source Hermes reads from
    # (env vars, external OAuth files, auth.json blocks, custom config)
    # has a RemovalStep registered in agent.credential_sources.  The step
    # handles its source-specific cleanup and we centralise suppression +
    # user-facing output here so every source behaves identically from
    # the user's perspective.
    from agent.credential_sources import find_removal_step
    from hermes_cli.auth import suppress_credential_source

    step = find_removal_step(provider, removed.source)
    if step is None:
        # Unregistered source — e.g. "manual", which has nothing external
        # to clean up.  The pool entry is already gone; we're done.
        return

    result = step.remove_fn(provider, removed)
    for line in result.cleaned:
        print(line)
    if result.suppress:
        suppress_credential_source(provider, removed.source)
    for line in result.hints:
        print(line)


def _redact_token(token) -> str:
    """Show enough of a token to distinguish accounts, never the value."""
    raw = str(token or "")
    if not raw:
        return "(none)"
    return f"{raw[:6]}…(len={len(raw)})"


def auth_codex_login_command(args) -> None:
    """`hermes auth codex login [--account <label>]` — device-code login into a slot."""
    account = auth_mod.normalize_codex_account_label(getattr(args, "account", None))
    creds = auth_mod._codex_device_code_login()
    label = (getattr(args, "label", None) or "").strip() or None
    if account == auth_mod.CODEX_PRIMARY_ACCOUNT:
        auth_mod._save_codex_tokens(creds["tokens"], creds.get("last_refresh"), label)
    else:
        auth_mod._save_codex_tokens(
            creds["tokens"], creds.get("last_refresh"), label, account=account,
        )
    # An explicit login re-enables a previously removed slot (same pattern as
    # the xai-oauth login path — removal leaves a suppression marker that
    # would otherwise block the re-seed silently).
    source = auth_mod.codex_account_source(account)
    auth_mod.unsuppress_credential_source("openai-codex", source)
    auth_mod.mark_provider_active_if_unset("openai-codex")
    # Reload the pool so the slot's entry is upserted immediately.
    pool = load_pool("openai-codex")
    entry = next((e for e in pool.entries() if e.source == source), None)
    print()
    print(f'Login successful — saved to Codex account "{account}".')
    if entry is not None:
        print(f'  Pool entry: id={entry.id} label="{entry.label}" source={entry.source}')
    from hermes_constants import display_hermes_home as _dhh
    print(f"  Auth state: {_dhh()}/auth.json")


def auth_codex_list_command(args) -> None:
    """`hermes auth codex list` — account slots, status, preference, selection."""
    del args
    accounts = auth_mod.list_codex_accounts()
    pool = load_pool("openai-codex")
    entries_by_source = {entry.source: entry for entry in pool.entries()}
    selected = pool.peek()

    if not any(acct["has_tokens"] for acct in accounts) and not pool.entries():
        print("No Codex credentials stored. Run `hermes auth codex login` first.")
        return

    print("openai-codex accounts:")
    for acct in accounts:
        entry = entries_by_source.get(acct["source"])
        if entry is not None:
            token = entry.access_token
            if entry.last_status == "dead":
                reason = getattr(entry, "last_error_reason", None) or "terminal auth failure"
                status = f"dead ({reason} — re-login required)"
            else:
                status = _format_exhausted_status(entry).strip() or "ok"
            status = f"{status}{_format_taxonomy_suffix(entry)}"
        elif acct["has_tokens"]:
            token = ""
            status = "ok (not yet seeded into pool)"
        else:
            token = ""
            if acct["account"] == auth_mod.CODEX_PRIMARY_ACCOUNT:
                status = "no tokens (run `hermes auth codex login`)"
            else:
                status = (
                    "no tokens (run `hermes auth codex login "
                    f"--account {acct['account']}`)"
                )
        preferred_marker = "*" if acct.get("preferred") else " "
        select_marker = ""
        if selected is not None and entry is not None and selected.id == entry.id:
            select_marker = "  ← pool would select"
        print(
            f'  {preferred_marker} {acct["account"]:<16} label="{acct["label"]}" '
            f"token={_redact_token(token)} {status}{select_marker}".rstrip()
        )
    print()
    print("  * = preferred account — change with `hermes auth codex switch <account>`")
    # Independent Codex credentials created by `hermes auth add openai-codex`
    # (source manual:device_code) are not account slots; when one of them
    # would win selection, say so instead of showing no selection marker.
    slot_sources = {acct["source"] for acct in accounts}
    if selected is not None and selected.source not in slot_sources:
        print(
            f'  note: the pool would currently select the independent credential '
            f'"{selected.label}" ({_display_source(selected.source)}, added via '
            "`hermes auth add openai-codex`) — not an account slot. Setting a "
            "preferred account ranks the slot above it."
        )


def auth_codex_switch_command(args) -> None:
    """`hermes auth codex switch <account>` — persist the preferred account."""
    account = auth_mod.normalize_codex_account_label(getattr(args, "account", None))
    try:
        auth_mod.set_codex_preferred_account(account)
    except auth_mod.AuthError as exc:
        raise SystemExit(str(exc))
    # Reload so the pool's priority order reflects the new preference now.
    load_pool("openai-codex")
    print(f'Preferred Codex account set to "{account}".')
    print("No restart needed for the codex proxy — it resolves credentials per")
    print("request. Long-running agent sessions pick the preference up on their")
    print("next credential-pool reload; restart them to apply it immediately.")


def auth_codex_command(args) -> None:
    action = str(getattr(args, "codex_action", "") or "").strip().lower()
    if action == "login":
        auth_codex_login_command(args)
        return
    if action == "switch":
        auth_codex_switch_command(args)
        return
    # Default (including bare `hermes auth codex`): list.
    auth_codex_list_command(args)


def auth_reset_command(args) -> None:
    provider = _normalize_provider(getattr(args, "provider", ""))
    pool = load_pool(provider)
    count = pool.reset_statuses()
    print(f"Reset status on {count} {provider} credentials")


def _print_pool_taxonomy(provider: str) -> None:
    """Print any taxonomy fields (86e2mb8nv/86e2mb8p0/86e2mb8p5) recorded
    against ``provider``'s credential-pool entries: ``last_failure_kind``,
    the out-of-band probe verdict, and usage headroom.

    Read-only via ``agent.credential_pool.failure_summary()`` (never seeds,
    refreshes, or mutates the pool) and safe to call regardless of whether
    ``get_auth_status`` reports logged in or out — a dead/quarantined pool
    entry's ``last_failure_kind`` is often exactly why a provider shows
    logged out, and is useful there too. Silently prints nothing when the
    provider has no pool entries or none carry a taxonomy field (most
    entries, and every ``PooledCredential`` written before 86e2mb8nv
    landed, carry none).
    """
    try:
        from agent.credential_pool import failure_summary

        summary = failure_summary(provider)
    except Exception:
        return
    rows = summary.get(provider) or []
    printed_header = False
    for row in rows:
        suffix = _format_taxonomy_suffix(SimpleNamespace(
            last_failure_kind=row.get("last_failure_kind"),
            last_probe_verdict=row.get("last_probe_verdict"),
            usage_percent=row.get("usage_percent"),
        ))
        if not suffix:
            continue
        if not printed_header:
            print("  taxonomy:")
            printed_header = True
        label = row.get("label") or row.get("id") or "?"
        print(f"    {label}:{suffix}")


def auth_status_command(args) -> None:
    provider = _normalize_provider(getattr(args, "provider", "") or "")
    if not provider:
        raise SystemExit("Provider is required. Example: `hermes auth status spotify`.")
    status = auth_mod.get_auth_status(provider)
    if not status.get("logged_in"):
        reason = status.get("error")
        if reason:
            print(f"{provider}: logged out ({reason})")
        else:
            print(f"{provider}: logged out")
        _print_pool_taxonomy(provider)
        return

    print(f"{provider}: logged in")
    for key in ("auth_type", "client_id", "redirect_uri", "scope", "expires_at", "api_base_url"):
        value = status.get(key)
        if value:
            print(f"  {key}: {value}")
    _print_pool_taxonomy(provider)


def auth_logout_command(args) -> None:
    auth_mod.logout_command(SimpleNamespace(provider=getattr(args, "provider", None)))


def auth_spotify_command(args) -> None:
    action = str(getattr(args, "spotify_action", "") or "login").strip().lower()
    if action in {"", "login"}:
        auth_mod.login_spotify_command(args)
        return
    if action == "status":
        auth_status_command(SimpleNamespace(provider="spotify"))
        return
    if action == "logout":
        auth_logout_command(SimpleNamespace(provider="spotify"))
        return
    raise SystemExit(f"Unknown Spotify auth action: {action}")


def _interactive_auth() -> None:
    """Interactive credential pool management when `hermes auth` is called bare."""
    # Show current pool status first
    print("Credential Pool Status")
    print("=" * 50)

    auth_list_command(SimpleNamespace(provider=None))

    # Show AWS Bedrock credential status (not in the pool — uses boto3 chain)
    try:
        from agent.bedrock_adapter import has_aws_credentials, resolve_aws_auth_env_var, resolve_bedrock_region
        if has_aws_credentials():
            auth_source = resolve_aws_auth_env_var() or "unknown"
            region = resolve_bedrock_region()
            print("bedrock (AWS SDK credential chain):")
            print(f"  Auth: {auth_source}")
            print(f"  Region: {region}")
            try:
                import boto3
                sts = boto3.client("sts", region_name=region)
                identity = sts.get_caller_identity()
                arn = identity.get("Arn", "unknown")
                print(f"  Identity: {arn}")
            except Exception:
                print("  Identity: (could not resolve — boto3 STS call failed)")
            print()
    except ImportError:
        pass  # boto3 or bedrock_adapter not available

    # Show Azure Foundry Entra ID status
    try:
        from hermes_cli.config import load_config
        _cfg = load_config()
        _model_cfg = _cfg.get("model") if isinstance(_cfg, dict) else None
        if isinstance(_model_cfg, dict):
            _cfg_provider = str(_model_cfg.get("provider") or "").strip().lower()
            _cfg_auth_mode = str(_model_cfg.get("auth_mode") or "").strip().lower()
            if _cfg_provider == "azure-foundry" and _cfg_auth_mode == "entra_id":
                from agent.azure_identity_adapter import (
                    EntraIdentityConfig,
                    SCOPE_AI_AZURE_DEFAULT,
                    describe_active_credential,
                    has_azure_identity_installed,
                )
                _base_url = str(_model_cfg.get("base_url") or "").strip()
                _entra = _model_cfg.get("entra") or {}
                if not isinstance(_entra, dict):
                    _entra = {}
                _scope = (
                    str(_entra.get("scope") or "").strip()
                    or SCOPE_AI_AZURE_DEFAULT
                )
                print("azure-foundry (Microsoft Entra ID):")
                print(f"  Endpoint: {_base_url or '(not configured)'}")
                print(f"  Scope: {_scope}")
                if not has_azure_identity_installed():
                    print("  Status: ⚠ azure-identity not installed "
                          "(pip install azure-identity)")
                else:
                    _entra_cfg = EntraIdentityConfig(
                        scope=_scope,
                    )
                    _info = describe_active_credential(config=_entra_cfg, timeout_seconds=10.0)
                    _env_sources = _info.get("env_sources") or []
                    if _info.get("ok"):
                        _tag = ", ".join(_env_sources) if _env_sources else "default chain"
                        print(f"  Status: ✓ token acquired ({_tag})")
                    else:
                        _err = _info.get("error") or "credential chain exhausted"
                        print(f"  Status: ⚠ {_err}")
                        _hint = _info.get("hint")
                        if _hint:
                            print(f"  Hint: {_hint}")
                print()
    except Exception:
        pass
    print()

    # Main menu
    choices = [
        "Add a credential",
        "Remove a credential",
        "Reset cooldowns for a provider",
        "Set rotation strategy for a provider",
        "Exit",
    ]
    print("What would you like to do?")
    for i, choice in enumerate(choices, 1):
        print(f"  {i}. {choice}")

    try:
        raw = input("\nChoice: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not raw or raw == str(len(choices)):
        return

    if raw == "1":
        _interactive_add()
    elif raw == "2":
        _interactive_remove()
    elif raw == "3":
        _interactive_reset()
    elif raw == "4":
        _interactive_strategy()


def _pick_provider(prompt: str = "Provider") -> str:
    """Prompt for a provider name with auto-complete hints."""
    known = sorted(set(list(PROVIDER_REGISTRY.keys()) + ["openrouter"]))
    custom_names = _get_custom_provider_names()
    if custom_names:
        custom_display = [name for name, _key, _provider_key in custom_names]
        print(f"\nKnown providers: {', '.join(known)}")
        print(f"Custom endpoints: {', '.join(custom_display)}")
    else:
        print(f"\nKnown providers: {', '.join(known)}")
    try:
        raw = input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit()
    return _normalize_provider(raw)


def _interactive_add() -> None:
    provider = _pick_provider("Provider to add credential for")
    if provider not in PROVIDER_REGISTRY and provider != "openrouter" and not provider.startswith(CUSTOM_POOL_PREFIX):
        raise SystemExit(f"Unknown provider: {provider}")

    # For OAuth-capable providers, ask which type
    if provider in _OAUTH_CAPABLE_PROVIDERS:
        print(f"\n{provider} supports both API keys and OAuth login.")
        print("  1. API key (paste a key from the provider dashboard)")
        print("  2. OAuth login (authenticate via browser)")
        try:
            type_choice = input("Type [1/2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if type_choice == "2":
            auth_type = "oauth"
        else:
            auth_type = "api_key"
    else:
        auth_type = "api_key"

    label = None
    try:
        typed_label = input("Label / account name (optional): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if typed_label:
        label = typed_label

    auth_add_command(SimpleNamespace(
        provider=provider, auth_type=auth_type, label=label, api_key=None,
        portal_url=None, inference_url=None, client_id=None, scope=None,
        no_browser=False, timeout=None, insecure=False, ca_bundle=None,
    ))


def _interactive_remove() -> None:
    provider = _pick_provider("Provider to remove credential from")
    pool = load_pool(provider)
    if not pool.has_credentials():
        print(f"No credentials for {provider}.")
        return

    # Show entries with indices
    for i, e in enumerate(pool.entries(), 1):
        exhausted = _format_exhausted_status(e)
        print(f"  #{i}  {e.label:25s} {e.auth_type:10s} {e.source}{exhausted} [id:{e.id}]")

    try:
        raw = input("Remove #, id, or label (blank to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not raw:
        return

    auth_remove_command(SimpleNamespace(provider=provider, target=raw))


def _interactive_reset() -> None:
    provider = _pick_provider("Provider to reset cooldowns for")

    auth_reset_command(SimpleNamespace(provider=provider))


def _interactive_strategy() -> None:
    provider = _pick_provider("Provider to set strategy for")
    current = get_pool_strategy(provider)
    strategies = [STRATEGY_FILL_FIRST, STRATEGY_ROUND_ROBIN, STRATEGY_LEAST_USED, STRATEGY_RANDOM]

    print(f"\nCurrent strategy for {provider}: {current}")
    print()
    descriptions = {
        STRATEGY_FILL_FIRST: "Use first key until exhausted, then next",
        STRATEGY_ROUND_ROBIN: "Cycle through keys evenly",
        STRATEGY_LEAST_USED: "Always pick the least-used key",
        STRATEGY_RANDOM: "Random selection",
    }
    for i, s in enumerate(strategies, 1):
        marker = " ←" if s == current else ""
        print(f"  {i}. {s:15s} — {descriptions.get(s, '')}{marker}")

    try:
        raw = input("\nStrategy [1-4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not raw:
        return

    try:
        idx = int(raw) - 1
        strategy = strategies[idx]
    except (ValueError, IndexError):
        print("Invalid choice.")
        return

    from hermes_cli.config import load_config, save_config
    cfg = load_config()
    pool_strategies = cfg.get("credential_pool_strategies") or {}
    if not isinstance(pool_strategies, dict):
        pool_strategies = {}
    pool_strategies[provider] = strategy
    cfg["credential_pool_strategies"] = pool_strategies
    save_config(cfg)
    print(f"Set {provider} strategy to: {strategy}")


def auth_command(args) -> None:
    action = getattr(args, "auth_action", "")
    if action == "add":
        auth_add_command(args)
        return
    if action == "list":
        auth_list_command(args)
        return
    if action == "remove":
        auth_remove_command(args)
        return
    if action == "reset":
        auth_reset_command(args)
        return
    if action == "status":
        auth_status_command(args)
        return
    if action == "logout":
        auth_logout_command(args)
        return
    if action == "spotify":
        auth_spotify_command(args)
        return
    if action == "codex":
        auth_codex_command(args)
        return
    # No subcommand — launch interactive mode
    _interactive_auth()
