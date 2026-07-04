"""OpenAI Codex (ChatGPT) OAuth upstream adapter.

Forwards OpenAI **Responses API** requests to the ChatGPT-backed Codex endpoint
(``https://chatgpt.com/backend-api/codex``) using Hermes' own OAuth credential
store. This lets any local OpenAI-compatible client that speaks the Responses
shape (``/v1/responses``) write through the subscription-flat Codex backend.

Unlike the nous/xai adapters, the Codex backend sits behind Cloudflare and
rejects requests that don't advertise a first-party originator. We therefore
attach three non-standard upstream headers via ``UpstreamCredential.extra_headers``:

  - ``originator: codex_cli_rs``         (Cloudflare all-list)
  - ``User-Agent: codex_cli_rs/...``     (SDK-fingerprint evasion)
  - ``ChatGPT-Account-ID: <jwt claim>``  (account routing, from the OAuth JWT)

Token resolution + refresh (including the cross-process refresh lock that
prevents a single-use refresh_token from being double-spent) is delegated to
``hermes_cli.auth.resolve_codex_runtime_credentials`` — the same function
Hermes' native chat path uses. The proxy server forwards the request/response
body verbatim, so no chat<->responses translation happens here: the client is
expected to send the Responses shape directly (e.g. gpt-5.4 via the OpenAI
Responses API). gpt-5.4 is the silent-hang-safe Codex model; gpt-5.5 on the
Codex backend has a documented silent-hang risk — prefer gpt-5.4.
"""

# HERMES-PATCH 26: codex-proxy-writer — local OpenAI-compatible upstream adapter
# exposing the ChatGPT Codex OAuth backend (gpt-5.4) so OpenCode can WRITE code
# through the subscription-flat Codex endpoint. Re-applied by
# ~/.hermes/scripts/verify-hermes-patches.sh after `hermes update`.
from __future__ import annotations

import base64
import json
import logging
import threading
from typing import Dict, FrozenSet, Optional

from hermes_cli.auth import resolve_codex_runtime_credentials
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential

logger = logging.getLogger(__name__)

# Codex is a Responses-API backend. ``/responses`` is the live path; the others
# are advertised so a misrouted ``/models`` probe gets a clean upstream answer
# rather than a 404 from the proxy. The Codex backend itself decides what it
# serves — the proxy just allows the path through.
_ALLOWED_PATHS: FrozenSet[str] = frozenset(
    {
        "/responses",
        "/models",
    }
)

# Fields the Codex backend rejects ("Unsupported parameter") that clients
# (notably OpenCode's Responses provider) emit unavoidably. Stripped in
# ``transform_request_body``.
_CODEX_REJECTED_FIELDS: FrozenSet[str] = frozenset({"max_output_tokens"})


def _codex_cloudflare_headers(access_token: str) -> Dict[str, str]:
    """Headers required to avoid Cloudflare 403s on chatgpt.com/backend-api/codex.

    Inlined from ``agent.auxiliary_client._codex_cloudflare_headers`` (kept in
    sync deliberately) so the proxy adapter stays self-contained and doesn't
    pull the heavy auxiliary_client module into the proxy process. Malformed
    tokens are tolerated — we drop the account-ID header rather than raise, so a
    bad token surfaces as an upstream 401 instead of crashing the forwarder.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
        "originator": "codex_cli_rs",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return headers
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(acct_id, str) and acct_id:
            headers["ChatGPT-Account-ID"] = acct_id
    except Exception:
        pass
    return headers


class OpenAICodexAdapter(UpstreamAdapter):
    """Proxy upstream for the ChatGPT Codex backend via Hermes-managed OAuth."""

    auth_hint = "hermes auth add openai-codex --type oauth"

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "openai-codex"

    @property
    def display_name(self) -> str:
        return "OpenAI Codex OAuth"

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    def is_authenticated(self) -> bool:
        # Cheap, no network: resolve WITHOUT triggering a refresh and report
        # whether a usable access token exists in the store/pool.
        try:
            creds = resolve_codex_runtime_credentials(refresh_if_expiring=False)
        except Exception:
            return False
        return bool((creds or {}).get("api_key"))

    def get_credential(self) -> UpstreamCredential:
        with self._lock:
            return self._resolve(force_refresh=False)

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> Optional[UpstreamCredential]:
        # On a 401 the access token is likely stale — force a refresh once and
        # retry. (429 = quota, not auth; let it flow back to the client.)
        if status_code != 401:
            return None
        with self._lock:
            try:
                retry = self._resolve(force_refresh=True)
            except Exception as exc:
                logger.warning("proxy: codex forced refresh failed: %s", exc)
                return None
            if retry.bearer == failed_credential.bearer:
                return None
            logger.info("proxy: codex upstream returned 401; retrying with refreshed token")
            return retry

    def transform_request_body(self, rel_path: str, body: bytes) -> bytes:
        """Drop request fields the Codex backend rejects.

        OpenCode's built-in OpenAI Responses provider hardcodes a
        ``max_output_tokens`` fallback (8192) on every request — there's no
        client-side knob to suppress it — and the Codex backend rejects it with
        ``Unsupported parameter: max_output_tokens``. We strip exactly that
        field (and nothing else); the body is otherwise forwarded verbatim.
        Tolerant: returns the body unchanged on non-JSON or any parse error.
        """
        if not body or rel_path != "/responses":
            return body
        try:
            payload = json.loads(body)
        except Exception:
            return body
        if not isinstance(payload, dict):
            return body
        removed = False
        for key in _CODEX_REJECTED_FIELDS:
            if key in payload:
                payload.pop(key, None)
                removed = True
        if not removed:
            return body
        return json.dumps(payload).encode("utf-8")

    def _resolve(self, *, force_refresh: bool) -> UpstreamCredential:
        creds = resolve_codex_runtime_credentials(force_refresh=force_refresh)
        access_token = str((creds or {}).get("api_key", "") or "").strip()
        if not access_token:
            raise RuntimeError(
                "No usable OpenAI Codex OAuth credential. Run "
                "`hermes auth add openai-codex --type oauth` first."
            )
        base_url = str((creds or {}).get("base_url", "") or "").strip().rstrip("/")
        if not base_url:
            raise RuntimeError("Codex credential resolved without a base_url.")
        return UpstreamCredential(
            bearer=access_token,
            base_url=base_url,
            extra_headers=_codex_cloudflare_headers(access_token),
        )


__all__ = ["OpenAICodexAdapter"]
