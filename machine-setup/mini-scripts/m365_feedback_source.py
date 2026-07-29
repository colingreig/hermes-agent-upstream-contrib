"""Guarded Microsoft Graph source for a controlled spam-report mailbox.

This module is wired through the main PurelyMail poller, which owns durable
delta-cursor persistence, report disposition, deduplication, and acknowledgement
policy.  Production config keeps the Graph source disabled until its documented
external activation controls are complete.

Least-privilege app-only assumptions
------------------------------------
The Entra application is expected to have only the Microsoft Graph
``Mail.Read`` *application* permission (``Mail.ReadBasic.All`` cannot fetch
raw MIME).  Exchange Online Application RBAC or an Application Access Policy
must restrict that application to the one configured in-organization
reporting mailbox.  Do not grant ``Mail.ReadWrite`` or broad mutation scopes.
Admin consent, secret storage, mailbox restriction, and the dedicated report
folder are deployment responsibilities; the main poller owns durable cursor
and report-disposition state.

The implementation uses client credentials, GET-only Graph mailbox calls,
and never deletes, moves, marks read, or acknowledges a message.  Tokens and
raw MIME bytes are never logged or included in object representations.
"""

from __future__ import annotations

import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Protocol


TOKEN_HOST = "login.microsoftonline.com"
GRAPH_HOST = "graph.microsoft.com"
GRAPH_ROOT = f"https://{GRAPH_HOST}/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_PAGE_SIZE = 100
MAX_MIME_BYTES = 100 * 1024 * 1024
MAX_CURSOR_LENGTH = 32 * 1024
MAX_FOLDER_ID_LENGTH = 1024
MAX_MESSAGE_ID_LENGTH = 2048
MAX_ENV_NAME_LENGTH = 128
MAX_ORGANIZATION_DOMAINS = 32
ALLOWED_CURSOR_QUERY_FIELDS = frozenset({
    "$skiptoken", "$deltatoken", "$select", "$top", "$filter", "$orderby",
    "$expand", "$count", "$skip", "$search",
})
CONTINUATION_FIELDS = frozenset({"$skiptoken", "$deltatoken"})


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


class SourceHealth(str, Enum):
    """Health attached to successful results and typed failures."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class GraphFeedbackError(RuntimeError):
    """Base failure that is safe to expose without leaking token or body."""

    def __init__(
        self,
        message: str,
        *,
        health: SourceHealth,
        transient: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.health = health
        self.transient = transient
        self.status = status


class ConfigurationError(GraphFeedbackError):
    def __init__(self, message: str) -> None:
        super().__init__(message, health=SourceHealth.UNAVAILABLE)


class AuthenticationError(GraphFeedbackError):
    pass


class TransportError(GraphFeedbackError):
    pass


class ProtocolError(GraphFeedbackError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


class HttpTransport(Protocol):
    """Deterministic HTTP injection boundary used by tests and production."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


class UrllibTransport:
    """TLS-verifying stdlib transport with bounded response reads."""

    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        """Never construct a redirected request that could inherit credentials."""

        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: object,
            code: int,
            msg: str,
            headers: Mapping[str, str],
            newurl: str,
        ) -> None:
            del req, fp, code, msg, headers, newurl
            return None

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context),
            self._NoRedirectHandler(),
        )

    @staticmethod
    def _read_bounded(response: object, limit: int) -> bytes:
        data = response.read(limit + 1)  # type: ignore[attr-defined]
        if len(data) > limit:
            raise ValueError("HTTP response exceeds configured size limit")
        return data

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("HTTPS is required")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                final_url = response.geturl()
                if not isinstance(final_url, str) or final_url != request.full_url:
                    raise ValueError("HTTP response final URL did not match the requested endpoint")
                return HttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=self._read_bounded(response, max_response_bytes),
                )
        except urllib.error.HTTPError as exc:
            final_url = exc.geturl()
            if not isinstance(final_url, str) or final_url != request.full_url:
                raise ValueError("HTTP error final URL did not match the requested endpoint") from exc
            return HttpResponse(
                status=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=self._read_bounded(exc, max_response_bytes),
            )


@dataclass(frozen=True)
class M365FeedbackConfig:
    tenant_id: str
    client_id: str
    client_secret_env: str
    reporting_mailbox: str
    folder_id: str
    organization_domains: tuple[str, ...]
    page_size: int = 25
    timeout_seconds: float = 20.0
    max_retries: int = 2
    max_retry_delay_seconds: float = 30.0
    max_mime_bytes: int = 25 * 1024 * 1024

    def validate(self) -> None:
        for label, value in (("tenant_id", self.tenant_id), ("client_id", self.client_id)):
            if (
                not isinstance(value, str) or len(value) != 36
                or _has_control_characters(value)
            ):
                raise ConfigurationError(f"{label} must be an exact UUID")
            try:
                parsed = uuid.UUID(value)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ConfigurationError(f"{label} must be an exact UUID") from exc
            if str(parsed) != value.lower() or value.startswith("{"):
                raise ConfigurationError(f"{label} must be a canonical UUID")

        if (
            not isinstance(self.client_secret_env, str)
            or len(self.client_secret_env) > MAX_ENV_NAME_LENGTH
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.client_secret_env)
        ):
            raise ConfigurationError("client_secret_env must be one exact environment name")

        if not isinstance(self.organization_domains, tuple) or any(
            not isinstance(domain, str) for domain in self.organization_domains
        ):
            raise ConfigurationError("organization_domains must be an exact tuple of domains")
        domains = tuple(domain.strip().lower() for domain in self.organization_domains)
        if (
            not domains or len(domains) > MAX_ORGANIZATION_DOMAINS
            or len(set(domains)) != len(domains) or any(
            len(domain) > 253
            or _has_control_characters(domain)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
            or "." not in domain
            or "*" in domain
            for domain in domains
            )
        ):
            raise ConfigurationError("organization_domains must contain exact DNS domains")

        mailbox = self.reporting_mailbox
        if (
            not isinstance(mailbox, str)
            or len(mailbox) > 254
            or _has_control_characters(mailbox)
            or mailbox != mailbox.strip().lower()
            or mailbox.count("@") != 1
        ):
            raise ConfigurationError("reporting_mailbox must be one exact lowercase address")
        local, domain = mailbox.rsplit("@", 1)
        if (
            not local
            or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or domain not in domains
            or "*" in mailbox
            or any(ch.isspace() for ch in mailbox)
            or mailbox in {"all", "all@all"}
        ):
            raise ConfigurationError("reporting_mailbox must be an exact in-organization mailbox")

        folder = self.folder_id
        if (
            not isinstance(folder, str)
            or len(folder) > MAX_FOLDER_ID_LENGTH
            or _has_control_characters(folder)
            or folder != folder.strip()
            or not folder
            or any(character in folder for character in "*?#/\\")
            or ".." in folder
            or folder.lower() in {"all", "allitems", "msgfolderroot"}
        ):
            raise ConfigurationError("folder_id must identify one exact mailbox folder")

        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= MAX_PAGE_SIZE
        ):
            raise ConfigurationError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 120
        ):
            raise ConfigurationError("timeout_seconds must be in (0, 120]")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or not 0 <= self.max_retries <= 5
        ):
            raise ConfigurationError("max_retries must be between 0 and 5")
        if (
            isinstance(self.max_retry_delay_seconds, bool)
            or not isinstance(self.max_retry_delay_seconds, (int, float))
            or not math.isfinite(self.max_retry_delay_seconds)
            or not 0 <= self.max_retry_delay_seconds <= 60
        ):
            raise ConfigurationError("max_retry_delay_seconds must be between 0 and 60")
        if (
            isinstance(self.max_mime_bytes, bool)
            or not isinstance(self.max_mime_bytes, int)
            or not 1 <= self.max_mime_bytes <= MAX_MIME_BYTES
        ):
            raise ConfigurationError(f"max_mime_bytes must be between 1 and {MAX_MIME_BYTES}")


@dataclass(frozen=True)
class MessageReference:
    message_id: str
    internet_message_id: str | None
    received_at: str | None
    subject: str | None
    has_attachments: bool
    removed: bool


@dataclass(frozen=True)
class PollResult:
    messages: tuple[MessageReference, ...]
    cursor: str
    complete: bool
    health: SourceHealth = SourceHealth.HEALTHY


@dataclass(frozen=True)
class RawMimeResult:
    message_id: str
    raw_mime: bytes = field(repr=False)
    health: SourceHealth = SourceHealth.HEALTHY

    def __repr__(self) -> str:
        return (
            f"RawMimeResult(message_id={self.message_id!r}, "
            f"raw_mime=<redacted {len(self.raw_mime)} bytes>, health={self.health!r})"
        )


@dataclass(frozen=True)
class _AccessToken:
    value: str
    expires_at: float

    def __repr__(self) -> str:
        return "_AccessToken(value=<redacted>, expires_at=<redacted>)"


class M365FeedbackSource:
    """Read-only, exact-mailbox/folder Microsoft Graph feedback source."""

    def __init__(
        self,
        config: M365FeedbackConfig,
        *,
        transport: HttpTransport | None = None,
        environ: Mapping[str, str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config.validate()
        self.config = config
        self._transport = transport or UrllibTransport()
        if environ is None:
            import os

            environ = os.environ
        self._environ = environ
        self._sleep = sleeper
        self._clock = clock
        self._token: _AccessToken | None = None

        mailbox = urllib.parse.quote(config.reporting_mailbox, safe="")
        folder = urllib.parse.quote(config.folder_id, safe="")
        self._delta_path = f"/v1.0/users/{mailbox}/mailFolders/{folder}/messages/delta"
        self._delta_url = (
            f"https://{GRAPH_HOST}{self._delta_path}"
            "?$select=id,internetMessageId,receivedDateTime,subject,hasAttachments"
            f"&$top={config.page_size}"
        )
        self._message_root = f"{GRAPH_ROOT}/users/{mailbox}/messages"

    def _secret(self) -> str:
        secret = self._environ.get(self.config.client_secret_env)
        if not isinstance(secret, str) or not secret:
            raise AuthenticationError(
                "configured client secret environment variable is unavailable",
                health=SourceHealth.UNAVAILABLE,
            )
        return secret

    @staticmethod
    def _retry_delay(response: HttpResponse, attempt: int, cap: float) -> float:
        raw = next(
            (value for key, value in response.headers.items() if key.lower() == "retry-after"),
            None,
        )
        if raw is not None:
            try:
                return min(max(float(raw), 0.0), cap)
            except ValueError:
                pass
        return min(float(2**attempt), cap)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        max_response_bytes: int,
        auth_request: bool = False,
    ) -> HttpResponse:
        try:
            parsed = urllib.parse.urlsplit(url)
            parsed_port = parsed.port
        except ValueError as exc:
            raise ProtocolError(
                "refusing malformed Microsoft endpoint URL",
                health=SourceHealth.UNAVAILABLE,
            ) from exc
        allowed_host = TOKEN_HOST if auth_request else GRAPH_HOST
        if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed_port not in (None, 443):
            raise ProtocolError(
                "refusing request outside the fixed Microsoft HTTPS endpoint",
                health=SourceHealth.UNAVAILABLE,
            )
        if auth_request:
            if method != "POST":
                raise ProtocolError("token endpoint permits POST only", health=SourceHealth.UNAVAILABLE)
        elif method != "GET" or body is not None:
            raise ProtocolError("Graph mailbox source permits GET only", health=SourceHealth.UNAVAILABLE)

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._transport.request(
                    method,
                    url,
                    headers=headers,
                    body=body,
                    timeout=self.config.timeout_seconds,
                    max_response_bytes=max_response_bytes,
                )
            except (OSError, TimeoutError, socket.timeout, urllib.error.URLError, ValueError) as exc:
                if attempt >= self.config.max_retries:
                    raise TransportError(
                        "Microsoft endpoint transport failed",
                        health=SourceHealth.DEGRADED,
                        transient=True,
                    ) from exc
                self._sleep(min(float(2**attempt), self.config.max_retry_delay_seconds))
                continue

            if response.status in TRANSIENT_HTTP_STATUSES and attempt < self.config.max_retries:
                self._sleep(self._retry_delay(response, attempt, self.config.max_retry_delay_seconds))
                continue
            return response
        raise AssertionError("bounded retry loop exhausted unexpectedly")

    def _access_token(self) -> str:
        now = self._clock()
        if self._token is not None and self._token.expires_at > now + 60:
            return self._token.value

        token_url = (
            f"https://{TOKEN_HOST}/{urllib.parse.quote(self.config.tenant_id, safe='')}"
            "/oauth2/v2.0/token"
        )
        encoded = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "client_secret": self._secret(),
                "scope": GRAPH_SCOPE,
                "grant_type": "client_credentials",
            }
        ).encode("ascii")
        response = self._request(
            "POST",
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=encoded,
            max_response_bytes=MAX_JSON_BYTES,
            auth_request=True,
        )
        if response.status != 200:
            raise AuthenticationError(
                "Microsoft client-credentials authentication failed",
                health=(
                    SourceHealth.DEGRADED
                    if response.status in TRANSIENT_HTTP_STATUSES
                    else SourceHealth.UNAVAILABLE
                ),
                transient=response.status in TRANSIENT_HTTP_STATUSES,
                status=response.status,
            )
        try:
            payload = json.loads(response.body)
            token = payload["access_token"]
            expires_in = payload.get("expires_in", 3600)
            if not isinstance(token, str) or not token or isinstance(expires_in, bool):
                raise ValueError
            lifetime = max(1.0, float(expires_in))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationError(
                "Microsoft token response was invalid",
                health=SourceHealth.UNAVAILABLE,
            ) from exc
        self._token = _AccessToken(token, now + lifetime)
        return token

    def _graph_get(
        self,
        url: str,
        *,
        max_response_bytes: int,
        accept: str = "application/json",
    ) -> HttpResponse:
        response = self._request(
            "GET",
            url,
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Accept": accept,
            },
            body=None,
            max_response_bytes=max_response_bytes,
        )
        if response.status == 401:
            self._token = None
        if not 200 <= response.status < 300:
            transient = response.status in TRANSIENT_HTTP_STATUSES
            raise TransportError(
                "Microsoft Graph read failed",
                health=SourceHealth.DEGRADED if transient else SourceHealth.UNAVAILABLE,
                transient=transient,
                status=response.status,
            )
        return response

    def _validate_cursor(self, cursor: str) -> str:
        if (
            not isinstance(cursor, str) or not cursor
            or len(cursor) > MAX_CURSOR_LENGTH
            or _has_control_characters(cursor)
        ):
            raise ProtocolError("delta cursor must be a nonempty URL", health=SourceHealth.UNAVAILABLE)
        try:
            parsed = urllib.parse.urlsplit(cursor)
            parsed_port = parsed.port
        except ValueError as exc:
            raise ProtocolError("delta cursor URL is malformed", health=SourceHealth.UNAVAILABLE) from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != GRAPH_HOST
            or parsed_port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or urllib.parse.unquote(parsed.path) != urllib.parse.unquote(self._delta_path)
        ):
            raise ProtocolError(
                "delta cursor escaped the configured mailbox/folder",
                health=SourceHealth.UNAVAILABLE,
            )
        try:
            query_pairs = urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True, strict_parsing=True,
            )
        except ValueError as exc:
            raise ProtocolError(
                "delta cursor query is malformed", health=SourceHealth.UNAVAILABLE,
            ) from exc
        query: dict[str, list[str]] = {}
        for key, value in query_pairs:
            if (
                key not in ALLOWED_CURSOR_QUERY_FIELDS
                or not value
                or _has_control_characters(key)
                or _has_control_characters(value)
            ):
                raise ProtocolError(
                    "delta cursor contains an unexpected query field",
                    health=SourceHealth.UNAVAILABLE,
                )
            query.setdefault(key, []).append(value)
        continuation_keys = [key for key in CONTINUATION_FIELDS if key in query]
        if (
            len(continuation_keys) != 1
            or len(query[continuation_keys[0]]) != 1
            or any(len(values) != 1 for values in query.values())
        ):
            raise ProtocolError(
                "delta cursor lacks one opaque continuation token",
                health=SourceHealth.UNAVAILABLE,
            )
        return cursor

    def poll(self, cursor: str | None = None) -> PollResult:
        """Read one bounded delta page; caller durably persists the cursor."""

        url = self._delta_url if cursor is None else self._validate_cursor(cursor)
        response = self._graph_get(url, max_response_bytes=MAX_JSON_BYTES)
        try:
            payload = json.loads(response.body)
            values = payload["value"]
            if not isinstance(values, list) or len(values) > self.config.page_size:
                raise ValueError("message page exceeds configured bound")
            messages: list[MessageReference] = []
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                    raise ValueError("message entry lacks an ID")
                messages.append(
                    MessageReference(
                        message_id=item["id"],
                        internet_message_id=(
                            item.get("internetMessageId")
                            if isinstance(item.get("internetMessageId"), str)
                            else None
                        ),
                        received_at=(
                            item.get("receivedDateTime")
                            if isinstance(item.get("receivedDateTime"), str)
                            else None
                        ),
                        subject=item.get("subject") if isinstance(item.get("subject"), str) else None,
                        has_attachments=item.get("hasAttachments") is True,
                        removed=isinstance(item.get("@removed"), dict),
                    )
                )
            next_link = payload.get("@odata.nextLink")
            delta_link = payload.get("@odata.deltaLink")
            if bool(next_link) == bool(delta_link):
                raise ValueError("delta page must contain exactly one continuation cursor")
            result_cursor = self._validate_cursor(next_link or delta_link)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError(
                "Microsoft Graph delta response was invalid",
                health=SourceHealth.UNAVAILABLE,
            ) from exc
        return PollResult(tuple(messages), result_cursor, complete=bool(delta_link))

    def fetch_raw_mime(self, message_id: str) -> RawMimeResult:
        """Fetch RFC 5322 MIME with original headers and report attachments."""

        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > MAX_MESSAGE_ID_LENGTH
            or "*" in message_id
            or _has_control_characters(message_id)
            or any(ch.isspace() for ch in message_id)
        ):
            raise ProtocolError("message_id is invalid", health=SourceHealth.UNAVAILABLE)
        encoded_id = urllib.parse.quote(message_id, safe="")
        response = self._graph_get(
            f"{self._message_root}/{encoded_id}/$value",
            max_response_bytes=self.config.max_mime_bytes,
            accept="message/rfc822",
        )
        content_type = next(
            (value for key, value in response.headers.items() if key.lower() == "content-type"),
            "",
        ).lower()
        if content_type and not (
            content_type.startswith("message/rfc822")
            or content_type.startswith("application/octet-stream")
        ):
            raise ProtocolError(
                "Microsoft Graph MIME response had an unexpected content type",
                health=SourceHealth.UNAVAILABLE,
            )
        if not response.body:
            raise ProtocolError("Microsoft Graph returned empty MIME", health=SourceHealth.UNAVAILABLE)
        return RawMimeResult(message_id=message_id, raw_mime=response.body)


__all__ = [
    "AuthenticationError",
    "ConfigurationError",
    "GraphFeedbackError",
    "HttpResponse",
    "HttpTransport",
    "M365FeedbackConfig",
    "M365FeedbackSource",
    "MessageReference",
    "PollResult",
    "ProtocolError",
    "RawMimeResult",
    "SourceHealth",
    "TransportError",
    "UrllibTransport",
]
