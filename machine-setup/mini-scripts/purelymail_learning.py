"""Dormant, guarded provider-learning seams for PurelyMail.

This module is wired through the main poller, while production config keeps
provider learning and ManageSieve disabled.  It never guesses an original
message, deletes mail, expunges, or invents a filtering rule.  Callers own
durable storage and must supply exact index records and versioned Sieve
template text.
"""

from __future__ import annotations

import difflib
import email
import hashlib
import imaplib
import json
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol


INDEX_SCHEMA_VERSION = 1
SIEVE_TEMPLATE_SCHEMA_VERSION = 1
MAX_MESSAGE_ID_CHARS = 998
MAX_HEADER_BYTES = 16 * 1024
MAX_SIEVE_BYTES = 1024 * 1024
MAX_SIEVE_LIST_LINES = 500
MAX_SIEVE_LINE_BYTES = 16 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


class LearningError(RuntimeError):
    """Base typed failure; messages never contain credentials or mail bodies."""


class IndexValidationError(LearningError):
    pass


class ExactMatchError(LearningError):
    pass


class ImapLearningError(LearningError):
    pass


class SieveError(LearningError):
    pass


class SieveConflictError(SieveError):
    pass


class LearningOperation(str, Enum):
    SPAM = "spam"
    HAM = "ham"


def _exact_mailbox(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip().lower() or value.count("@") != 1:
        raise IndexValidationError("mailbox must be one exact lowercase address")
    local, domain = value.rsplit("@", 1)
    if (
        not local
        or not domain
        or "." not in domain
        or "*" in value
        or "%" in value
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise IndexValidationError("mailbox must be one exact lowercase address")
    return value


def _exact_folder(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 255
        or "*" in value
        or "%" in value
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise IndexValidationError("folder must be one exact non-wildcard name")
    return value


def normalize_message_id(value: Any) -> str:
    """Validate one bracketed RFC Message-ID without normalization guessing."""
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > MAX_MESSAGE_ID_CHARS
        or not re.fullmatch(r"<[^<>@\s\"\\]+@[^<>@\s\"\\]+>", value)
    ):
        raise IndexValidationError("original Message-ID must be one exact bracketed ID")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IndexValidationError(f"{label} must be a positive integer")
    return value


def _iso_utc(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise IndexValidationError("indexed_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IndexValidationError("indexed_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise IndexValidationError("indexed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class MessageUidIndexRecord:
    """Exact source-mailbox/Message-ID to provider UID evidence."""

    source_mailbox: str
    original_message_id: str
    folder: str
    uid: int
    uidvalidity: int
    indexed_at: str
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)
    schema_version: int = INDEX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_mailbox", _exact_mailbox(self.source_mailbox))
        object.__setattr__(self, "original_message_id", normalize_message_id(self.original_message_id))
        object.__setattr__(self, "folder", _exact_folder(self.folder))
        _positive_int(self.uid, "uid")
        _positive_int(self.uidvalidity, "uidvalidity")
        object.__setattr__(self, "indexed_at", _iso_utc(self.indexed_at))
        if self.schema_version != INDEX_SCHEMA_VERSION:
            raise IndexValidationError("unsupported index record schema")
        if not isinstance(self.provenance, Mapping):
            raise IndexValidationError("provenance must be an object")
        encoded = json.dumps(dict(self.provenance), sort_keys=True, default=str).encode("utf-8")
        if len(encoded) > 16 * 1024:
            raise IndexValidationError("provenance exceeds size limit")

    @property
    def key(self) -> str:
        exact = f"{self.source_mailbox}\0{self.original_message_id}".encode("utf-8")
        return hashlib.sha256(exact).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_mailbox": self.source_mailbox,
            "original_message_id": self.original_message_id,
            "folder": self.folder,
            "uid": self.uid,
            "uidvalidity": self.uidvalidity,
            "indexed_at": self.indexed_at,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MessageUidIndexRecord":
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version", "source_mailbox", "original_message_id", "folder",
            "uid", "uidvalidity", "indexed_at", "provenance",
        }:
            raise IndexValidationError("index record must contain the exact schema")
        return cls(**dict(value))


def build_index_record(
    *,
    source_mailbox: str,
    original_message_id: str,
    folder: str,
    uid: int,
    uidvalidity: int,
    provenance: Mapping[str, Any],
    indexed_at: datetime | None = None,
) -> MessageUidIndexRecord:
    observed = indexed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise IndexValidationError("indexed_at must include a timezone")
    return MessageUidIndexRecord(
        source_mailbox=source_mailbox,
        original_message_id=original_message_id,
        folder=folder,
        uid=uid,
        uidvalidity=uidvalidity,
        indexed_at=observed.astimezone(timezone.utc).isoformat(),
        provenance=provenance,
    )


def index_record(
    records: Mapping[str, Mapping[str, Any]],
    *,
    source_mailbox: str,
    original_message_id: str,
) -> MessageUidIndexRecord:
    mailbox = _exact_mailbox(source_mailbox)
    message_id = normalize_message_id(original_message_id)
    key = hashlib.sha256(f"{mailbox}\0{message_id}".encode("utf-8")).hexdigest()
    raw = records.get(key)
    if raw is None:
        raise ExactMatchError("no exact indexed original exists")
    record = MessageUidIndexRecord.from_dict(raw)
    if record.source_mailbox != mailbox or record.original_message_id != message_id:
        raise ExactMatchError("index key does not match its exact record")
    return record


def with_index_record(
    records: Mapping[str, Mapping[str, Any]], record: MessageUidIndexRecord,
) -> dict[str, dict[str, Any]]:
    """Return a new durable index mapping; never mutates the caller's mapping."""
    updated = {str(key): dict(value) for key, value in records.items()}
    updated[record.key] = record.to_dict()
    return updated


class ImapClient(Protocol):
    tls_active: bool
    authenticated_mailbox: str

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[Any]]: ...
    def status(self, mailbox: str, names: str) -> tuple[str, list[Any]]: ...
    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...


class VerifiedImapClient:
    """Small TLS-verifying IMAP adapter; callers explicitly close/logout it."""

    tls_active = True

    def __init__(
        self,
        host: str,
        port: int,
        mailbox: str,
        password: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not host or any(ch.isspace() for ch in host):
            raise ImapLearningError("IMAP host is invalid")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ImapLearningError("IMAP port is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120
        ):
            raise ImapLearningError("IMAP timeout is invalid")
        address = _exact_mailbox(mailbox)
        self.authenticated_mailbox = address
        if not isinstance(password, str) or not password:
            raise ImapLearningError("IMAP credential is unavailable")
        context = ssl_context or ssl.create_default_context()
        try:
            self._client = imaplib.IMAP4_SSL(
                host, port, ssl_context=context, timeout=timeout,
            )
            self._client.login(address, password)
        except (OSError, socket.timeout, imaplib.IMAP4.error) as exc:
            raise ImapLearningError("TLS IMAP connection or authentication failed") from exc

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[Any]]:
        return self._client.select(mailbox, readonly=readonly)

    def status(self, mailbox: str, names: str) -> tuple[str, list[Any]]:
        return self._client.status(mailbox, names)

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        return self._client.uid(command, *args)

    def close(self) -> None:
        try:
            self._client.close()
        except imaplib.IMAP4.error:
            pass

    def logout(self) -> None:
        self._client.logout()


@dataclass(frozen=True)
class LearningCopyResult:
    operation: LearningOperation
    source_mailbox: str
    original_message_id: str
    source_folder: str
    destination_folder: str
    uid: int
    uidvalidity: int
    status: str
    provenance: Mapping[str, Any] = field(repr=False)


def _status_uidvalidity(data: list[Any]) -> int:
    raw = b" ".join(item for item in data if isinstance(item, bytes)).decode("ascii", "replace")
    match = re.search(r"\bUIDVALIDITY\s+(\d+)\b", raw, re.IGNORECASE)
    if not match:
        raise ImapLearningError("IMAP UIDVALIDITY response was invalid")
    return int(match.group(1))


def _fetch_message_id(data: list[Any]) -> str:
    chunks: list[bytes] = []
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            chunks.append(item[1])
        elif isinstance(item, bytes) and b"Message-ID:" in item:
            chunks.append(item)
    raw = b"".join(chunks)
    if not raw or len(raw) > MAX_HEADER_BYTES:
        raise ExactMatchError("matched Message-ID header was missing or oversized")
    parsed = email.message_from_bytes(raw)
    values = parsed.get_all("Message-ID", [])
    if len(values) != 1:
        raise ExactMatchError("matched message must have exactly one Message-ID header")
    return normalize_message_id(str(values[0]).strip())


def copy_for_learning(
    client: ImapClient,
    record: MessageUidIndexRecord,
    *,
    operation: LearningOperation,
    expected_mailbox: str,
    junk_folder: str = "Junk",
    inbox_folder: str = "INBOX",
) -> LearningCopyResult:
    """COPY one reverified original for explicit spam/ham provider learning.

    HAM copies the exact original from Junk into INBOX.  It intentionally does
    not delete or expunge the Junk copy; removal, if ever desired, is a separate
    operator policy outside this safe seam.
    """
    if not isinstance(operation, LearningOperation):
        raise ImapLearningError("operation must be explicitly spam or ham")
    mailbox = _exact_mailbox(expected_mailbox)
    junk = _exact_folder(junk_folder)
    inbox = _exact_folder(inbox_folder)
    if record.source_mailbox != mailbox:
        raise ExactMatchError("index record belongs to a different source mailbox")
    if getattr(client, "tls_active", False) is not True:
        raise ImapLearningError("TLS IMAP is required")
    if getattr(client, "authenticated_mailbox", None) != mailbox:
        raise ExactMatchError("IMAP session belongs to a different mailbox")

    if operation is LearningOperation.SPAM:
        source_folder = record.folder
        destination = junk
        if source_folder == junk:
            raise ExactMatchError("spam original is already indexed in Junk")
    else:
        if record.folder != junk:
            raise ExactMatchError("ham correction requires an original indexed in Junk")
        source_folder = junk
        destination = inbox

    status, _ = client.select(source_folder, readonly=False)
    if status != "OK":
        raise ImapLearningError("could not select the exact indexed folder")
    status, status_data = client.status(source_folder, "(UIDVALIDITY)")
    if status != "OK" or _status_uidvalidity(status_data) != record.uidvalidity:
        raise ExactMatchError("UIDVALIDITY no longer matches the durable index")

    status, search_data = client.uid(
        "search", None, "HEADER", "Message-ID", record.original_message_id,
    )
    if status != "OK":
        raise ImapLearningError("exact Message-ID search failed")
    found = [
        int(value)
        for chunk in search_data if isinstance(chunk, bytes)
        for value in chunk.split() if value.isdigit()
    ]
    if found != [record.uid]:
        raise ExactMatchError("Message-ID search did not yield exactly the indexed UID")

    status, header_data = client.uid(
        "fetch", str(record.uid), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
    )
    if status != "OK" or _fetch_message_id(header_data) != record.original_message_id:
        raise ExactMatchError("fetched UID did not contain the exact indexed Message-ID")

    status, copy_data = client.uid("copy", str(record.uid), destination)
    if status != "OK":
        raise ImapLearningError("guarded IMAP COPY failed")
    response_hint = next(
        (item.decode("ascii", "replace")[:300] for item in copy_data if isinstance(item, bytes)),
        "",
    )
    return LearningCopyResult(
        operation=operation,
        source_mailbox=mailbox,
        original_message_id=record.original_message_id,
        source_folder=source_folder,
        destination_folder=destination,
        uid=record.uid,
        uidvalidity=record.uidvalidity,
        status="copied",
        provenance={
            "index": record.to_dict(),
            "imap_copy_response": response_hint,
            "verified_exact_match": True,
            "delete_or_expunge_performed": False,
        },
    )


@dataclass(frozen=True)
class SieveScriptInfo:
    name: str
    active: bool


class SieveTransport(Protocol):
    tls_active: bool
    timeout_seconds: float

    def list_scripts(self) -> tuple[SieveScriptInfo, ...]: ...
    def get_script(self, name: str) -> str: ...
    def put_script(self, name: str, text: str) -> None: ...
    def set_active(self, name: str) -> None: ...


def _sieve_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 128
        or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
        or value in {".", ".."}
    ):
        raise SieveError("Sieve script name is invalid")
    return value


def _script_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise SieveError("Sieve template text must be supplied")
    if "\x00" in value or len(value.encode("utf-8")) > MAX_SIEVE_BYTES:
        raise SieveError("Sieve template text is invalid or oversized")
    return value


def sieve_hash(text: str) -> str:
    return hashlib.sha256(_script_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VersionedSieveTemplate:
    mailbox: str
    script_name: str
    version: str
    text: str = field(repr=False)
    schema_version: int = SIEVE_TEMPLATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "mailbox", _exact_mailbox(self.mailbox))
        object.__setattr__(self, "script_name", _sieve_name(self.script_name))
        if not isinstance(self.version, str) or not re.fullmatch(r"v[1-9][0-9]*", self.version):
            raise SieveError("template version must match v<positive integer>")
        object.__setattr__(self, "text", _script_text(self.text))
        if self.schema_version != SIEVE_TEMPLATE_SCHEMA_VERSION:
            raise SieveError("unsupported Sieve template schema")


@dataclass(frozen=True)
class SieveReadResult:
    mailbox: str
    script_name: str
    active: bool
    sha256: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class SieveDiffResult:
    mailbox: str
    current_script_name: str
    template_script_name: str
    template_version: str
    current_sha256: str
    proposed_sha256: str
    unified_diff: str = field(repr=False)


@dataclass(frozen=True)
class SieveApplyResult:
    mailbox: str
    previous_script_name: str
    previous_sha256: str
    backup_sha256: str
    applied_script_name: str
    applied_version: str
    applied_sha256: str
    status: str


class SieveTemplateManager:
    """Read/diff by default; applies only supplied, versioned templates."""

    def __init__(self, mailbox: str, transport: SieveTransport) -> None:
        self.mailbox = _exact_mailbox(mailbox)
        if getattr(transport, "tls_active", False) is not True:
            raise SieveError("TLS ManageSieve transport is required")
        timeout = getattr(transport, "timeout_seconds", None)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120
        ):
            raise SieveError("bounded ManageSieve timeout is required")
        self.transport = transport

    def list_scripts(self) -> tuple[SieveScriptInfo, ...]:
        scripts = self.transport.list_scripts()
        if len(scripts) > MAX_SIEVE_LIST_LINES:
            raise SieveError("ManageSieve script list exceeds bound")
        validated: list[SieveScriptInfo] = []
        for item in scripts:
            if not isinstance(item, SieveScriptInfo):
                raise SieveError("ManageSieve list result is invalid")
            validated.append(SieveScriptInfo(_sieve_name(item.name), item.active is True))
        if sum(1 for item in validated if item.active) > 1:
            raise SieveError("ManageSieve reported multiple active scripts")
        return tuple(validated)

    def read_script(self, name: str) -> SieveReadResult:
        exact_name = _sieve_name(name)
        scripts = self.list_scripts()
        matches = [item for item in scripts if item.name == exact_name]
        if len(matches) != 1:
            raise SieveError("exact Sieve script was not listed once")
        text = _script_text(self.transport.get_script(exact_name))
        return SieveReadResult(
            mailbox=self.mailbox,
            script_name=exact_name,
            active=matches[0].active,
            sha256=sieve_hash(text),
            text=text,
        )

    def diff_template(
        self, current_script_name: str, template: VersionedSieveTemplate,
    ) -> SieveDiffResult:
        if template.mailbox != self.mailbox:
            raise SieveConflictError("template belongs to a different mailbox")
        current = self.read_script(current_script_name)
        diff = "".join(difflib.unified_diff(
            current.text.splitlines(keepends=True),
            template.text.splitlines(keepends=True),
            fromfile=current.script_name,
            tofile=f"{template.script_name}@{template.version}",
        ))
        if len(diff.encode("utf-8")) > 2 * MAX_SIEVE_BYTES:
            raise SieveError("Sieve diff exceeds bound")
        return SieveDiffResult(
            mailbox=self.mailbox,
            current_script_name=current.script_name,
            template_script_name=template.script_name,
            template_version=template.version,
            current_sha256=current.sha256,
            proposed_sha256=sieve_hash(template.text),
            unified_diff=diff,
        )

    def apply_template(
        self,
        current_script_name: str,
        template: VersionedSieveTemplate,
        *,
        apply: bool = False,
        expected_current_hash: str,
        backup_text: str,
    ) -> SieveApplyResult:
        if apply is not True:
            raise SieveConflictError("Sieve mutation requires explicit apply=True")
        if template.mailbox != self.mailbox:
            raise SieveConflictError("template belongs to a different mailbox")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_current_hash or ""):
            raise SieveConflictError("expected current hash is invalid")
        if template.script_name == _sieve_name(current_script_name):
            raise SieveConflictError("versioned apply requires a distinct script name")
        backup = _script_text(backup_text)
        current = self.read_script(current_script_name)
        if not current.active:
            raise SieveConflictError("expected current script is not the active script")
        if current.sha256 != expected_current_hash:
            raise SieveConflictError("current Sieve hash changed")
        if backup != current.text:
            raise SieveConflictError("explicit backup text does not exactly match current script")
        if any(item.name == template.script_name for item in self.list_scripts()):
            raise SieveConflictError("versioned target script already exists")

        proposed_hash = sieve_hash(template.text)
        try:
            self.transport.put_script(template.script_name, template.text)
            self.transport.set_active(template.script_name)
            active = [item for item in self.list_scripts() if item.active]
            if len(active) != 1 or active[0].name != template.script_name:
                raise SieveError("new versioned Sieve template was not activated exactly")
            installed = self.read_script(template.script_name)
            if installed.sha256 != proposed_hash:
                raise SieveError("installed Sieve template hash mismatch")
        except Exception as apply_exc:
            # The caller supplied and hash-locked this exact backup before any
            # mutation. Restore the prior active script; never synthesize a rule.
            try:
                self.transport.put_script(current.script_name, backup)
                self.transport.set_active(current.script_name)
                restored = self.read_script(current.script_name)
                if not restored.active or restored.sha256 != current.sha256:
                    raise SieveError("Sieve rollback verification failed")
            except Exception as restore_exc:
                raise SieveError(
                    "Sieve apply failed and exact backup restoration also failed"
                ) from restore_exc
            if isinstance(apply_exc, SieveError):
                raise
            raise SieveError("Sieve apply failed; exact backup was restored") from apply_exc
        return SieveApplyResult(
            mailbox=self.mailbox,
            previous_script_name=current.script_name,
            previous_sha256=current.sha256,
            backup_sha256=sieve_hash(backup),
            applied_script_name=template.script_name,
            applied_version=template.version,
            applied_sha256=installed.sha256,
            status="applied",
        )


__all__ = [
    "ExactMatchError", "ImapLearningError", "IndexValidationError",
    "LearningCopyResult", "LearningError", "LearningOperation",
    "MessageUidIndexRecord", "SieveApplyResult", "SieveConflictError",
    "SieveDiffResult", "SieveError", "SieveReadResult", "SieveScriptInfo",
    "SieveTemplateManager", "SieveTransport", "VerifiedImapClient",
    "VersionedSieveTemplate", "build_index_record", "copy_for_learning",
    "index_record", "normalize_message_id", "sieve_hash", "with_index_record",
]
