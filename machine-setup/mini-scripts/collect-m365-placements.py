#!/usr/bin/env python3
"""Probe the M365 destination mailbox for observed placements.

Standalone, stdlib-only tool. Reads a labelled draft JSONL (``id``,
``mailbox``, ``message_id`` per record -- produced by ``collect-labels.py``)
and, for each record, asks Microsoft Graph (app-only client-credentials)
whether a message with that ``internetMessageId`` landed in the M365
destination mailbox, and if so which folder. It writes a placements JSONL
consumed by ``evaluate-spam.py`` (joined on ``id``).

Match key
---------
The PurelyMail poller's ``build_forward_message()`` (see
``purelymail-notify-poller.py``) deep-copies the original message and only
strips ``To``/``From``/``Reply-To``/``Subject``/``DKIM-Signature``/
``Return-Path``/``Bcc``. The original ``Message-ID`` header is never removed
or regenerated, so it survives on the forwarded copy unchanged. This tool
therefore matches on Graph's ``internetMessageId`` against the labelled
record's ``message_id`` (the original mailbox's ``Message-ID`` header) via
an exact ``$filter`` equality query. This is a reliable match key precisely
*because* the forward path preserves the header -- if that ever changes,
this match key must change with it.

Because two monitored PurelyMail mailboxes can legitimately receive mail
sharing the same Message-ID, a ``$filter`` query can return more than one
forwarded copy in the destination mailbox. The forward path always rewrites
the ``From`` header to the SOURCE mailbox's address, so ``find_placement()``
also selects on Graph's ``from`` address to disambiguate (see its docstring
for the exact selection rules); an unresolvable case is reported as
``m365_placement = "unknown"`` rather than risk attributing the wrong
mailbox's copy.

Credentials
-----------
Read from environment variables (never from the config file or CLI args):

  M365_PLACEMENT_TENANT_ID      Entra tenant ID (GUID)
  M365_PLACEMENT_CLIENT_ID      Entra application (client) ID (GUID)
  M365_PLACEMENT_CLIENT_SECRET  Entra application client secret

The app-only registration needs the Microsoft Graph ``Mail.Read``
*application* permission (least privilege: this tool only ever issues GET
requests) scoped to the destination mailbox via Exchange Online Application
Access Policy, mirroring the guidance in ``m365_feedback_source.py``.

Placement mapping
------------------
Each labelled record's forwarded message is looked up by folder
``wellKnownName``:

  inbox        -> "inbox"    (PRIMARY)
  junkemail    -> "junk"     (RECOVERABLE)
  deleteditems -> "quarantine" (RECOVERABLE)
  no message found via the filter query -> "not_found" (MISSING)
  anything else (archive, other folders, or a folder id that could not be
    resolved in the mailbox's folder tree) -> "other" (PRIMARY)
  a message was found but could not be unambiguously attributed to the
    record's source mailbox via its From address (see find_placement()'s
    selection rules) -> "unknown" (MISSING)

These placement tokens match the sets consumed by evaluate-spam.py's
``derive_outcome()``.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


TOKEN_HOST = "login.microsoftonline.com"
GRAPH_HOST = "graph.microsoft.com"
GRAPH_ROOT = f"https://{GRAPH_HOST}/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_RETRIES = 3
MAX_RETRY_DELAY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 20.0
REQUEST_DELAY_SECONDS = 0.2
MAX_FOLDERS = 5000
MAX_FOLDER_DEPTH = 20
MAX_ID_LENGTH = 256
MAX_MAILBOX_LENGTH = 254
MAX_MESSAGE_ID_LENGTH = 2048

# folder wellKnownName -> m365_placement token (see module docstring)
WELL_KNOWN_TO_PLACEMENT = {
    "inbox": "inbox",
    "junkemail": "junk",
    "deleteditems": "quarantine",
}

DEFAULT_CONFIG_PATH = Path(__file__).with_name("notify-poller.config.json")


class CollectorError(Exception):
    """Fatal error (config/auth/protocol) -- caller exits 2."""


@dataclass(frozen=True)
class FetchResult:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


# Injectable HTTP boundary: (method, url, headers, body) -> FetchResult.
# Tests supply a stub; production uses UrllibFetcher. This is a superset of
# the (url, headers) -> (status, headers, body) shape sketched in the spec --
# extended with method/body because the OAuth2 client-credentials token
# request is a POST with a form-encoded body, which a GET-only (url,
# headers) signature cannot express. See "Spec deviations" in the task
# writeup for the one-line rationale.
Fetcher = Callable[[str, str, Mapping[str, str], "bytes | None"], FetchResult]


class UrllibFetcher:
    """TLS-verifying stdlib fetcher with bounded response reads.

    Mirrors the TLS/cert handling and no-redirect posture of
    ``m365_feedback_source.UrllibTransport``: a verified default SSL
    context (never falls back to CERT_NONE) and redirects are never
    followed (so credentials/tokens can never leak to an unexpected host).
    """

    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
            del req, fp, code, msg, headers, newurl
            return None

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        ssl_context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_context),
            self._NoRedirectHandler(),
        )

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
    ) -> FetchResult:
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("HTTPS is required")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return FetchResult(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=self._read_bounded(response),
                )
        except urllib.error.HTTPError as exc:
            return FetchResult(
                status=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=self._read_bounded(exc),
            )

    @staticmethod
    def _read_bounded(response: Any) -> bytes:
        data = response.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError("HTTP response exceeds configured size limit")
        return data


def load_secrets_file(path: Path) -> None:
    """Parse KEY=value lines from ``path`` into os.environ.

    Same env-file format as ``purelymail-notify-poller.load_secrets_file``
    (never overrides an already-set variable; ``#`` comments/blank lines
    ignored; matching surrounding quotes stripped). Reimplemented locally
    (rather than imported) so this standalone tool has no import-time
    dependency on the poller module.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value


def _retry_delay(headers: Mapping[str, str], attempt: int) -> float:
    raw = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if raw is not None:
        try:
            return min(max(float(raw), 0.0), MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(float(2**attempt), MAX_RETRY_DELAY_SECONDS)


def _request(
    fetcher: Fetcher,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: bytes | None,
    sleeper: Callable[[float], None],
    allowed_host: str,
) -> FetchResult:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        raise CollectorError(f"refusing request outside the fixed {allowed_host} endpoint")

    last: FetchResult | None = None
    for attempt in range(MAX_RETRIES + 1):
        response = fetcher(method, url, headers, body)
        last = response
        if response.status in TRANSIENT_HTTP_STATUSES and attempt < MAX_RETRIES:
            sleeper(_retry_delay(response.headers, attempt))
            continue
        return response
    assert last is not None
    return last


def get_access_token(
    fetcher: Fetcher,
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    token_url = f"https://{TOKEN_HOST}/{urllib.parse.quote(tenant_id, safe='')}/oauth2/v2.0/token"
    encoded = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": GRAPH_SCOPE,
            "grant_type": "client_credentials",
        }
    ).encode("ascii")
    response = _request(
        fetcher, "POST", token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=encoded, sleeper=sleeper, allowed_host=TOKEN_HOST,
    )
    if response.status != 200:
        raise CollectorError(
            f"Microsoft client-credentials authentication failed (status {response.status})"
        )
    try:
        payload = json.loads(response.body)
        token = payload["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CollectorError("Microsoft token response was invalid") from exc
    return token


def graph_get(
    fetcher: Fetcher,
    url: str,
    token: str,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    response = _request(
        fetcher, "GET", url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        body=None, sleeper=sleeper, allowed_host=GRAPH_HOST,
    )
    if not 200 <= response.status < 300:
        raise CollectorError(f"Microsoft Graph read of {url!r} failed (status {response.status})")
    try:
        payload = json.loads(response.body)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, json.JSONDecodeError) as exc:
        raise CollectorError("Microsoft Graph response was not valid JSON") from exc
    return payload


@dataclass(frozen=True)
class FolderInfo:
    id: str
    well_known_name: str | None
    display_name: str | None


def _folder_page_url(mailbox_quoted: str, folder_id: str | None) -> str:
    select = "$select=id,displayName,wellKnownName,childFolderCount"
    if folder_id is None:
        return f"{GRAPH_ROOT}/users/{mailbox_quoted}/mailFolders?{select}&$top=100&includeHiddenFolders=true"
    encoded_folder = urllib.parse.quote(folder_id, safe="")
    return (
        f"{GRAPH_ROOT}/users/{mailbox_quoted}/mailFolders/{encoded_folder}/childFolders"
        f"?{select}&$top=100&includeHiddenFolders=true"
    )


def build_folder_map(
    fetcher: Fetcher,
    token: str,
    destination: str,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, FolderInfo]:
    """Fetch the full mailFolders tree once and cache it by folder id.

    BFS over top-level folders and their childFolders, bounded by
    MAX_FOLDERS/MAX_FOLDER_DEPTH so a pathological or malicious folder tree
    cannot cause unbounded Graph calls.
    """
    mailbox_quoted = urllib.parse.quote(destination, safe="")
    folder_map: dict[str, FolderInfo] = {}
    queue: list[tuple[str | None, int]] = [(None, 0)]
    while queue:
        folder_id, depth = queue.pop(0)
        if depth > MAX_FOLDER_DEPTH:
            continue
        url = _folder_page_url(mailbox_quoted, folder_id)
        pages_fetched = 0
        while url and pages_fetched < 50:
            pages_fetched += 1
            payload = graph_get(fetcher, url, token, sleeper=sleeper)
            values = payload.get("value")
            if not isinstance(values, list):
                raise CollectorError("Microsoft Graph mailFolders response was malformed")
            for item in values:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                if len(folder_map) >= MAX_FOLDERS:
                    raise CollectorError("mailbox folder tree exceeds the configured bound")
                info = FolderInfo(
                    id=item["id"],
                    well_known_name=(
                        item.get("wellKnownName") if isinstance(item.get("wellKnownName"), str) else None
                    ),
                    display_name=(
                        item.get("displayName") if isinstance(item.get("displayName"), str) else None
                    ),
                )
                folder_map[info.id] = info
                if isinstance(item.get("childFolderCount"), int) and item["childFolderCount"] > 0:
                    queue.append((info.id, depth + 1))
            next_link = payload.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) else None
    return folder_map


def _odata_escape(value: str) -> str:
    return value.replace("'", "''")


def _from_address(item: Any) -> str | None:
    """Extract and normalize the From address off a Graph message resource."""
    if not isinstance(item, dict):
        return None
    from_obj = item.get("from")
    if not isinstance(from_obj, dict):
        return None
    email_address = from_obj.get("emailAddress")
    if not isinstance(email_address, dict):
        return None
    address = email_address.get("address")
    if not isinstance(address, str) or not address.strip():
        return None
    return address.strip().lower()


def find_placement(
    fetcher: Fetcher,
    token: str,
    destination: str,
    message_id: str,
    folder_map: dict[str, FolderInfo],
    *,
    source_mailbox: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[str, str | None]:
    """Return (m365_placement, m365_folder) for one internetMessageId.

    Selection rules (misattribution guard)
    ---------------------------------------
    Two monitored PurelyMail mailboxes can receive mail with the same
    Message-ID; both forwarded copies land in this one destination mailbox
    and an unconditional values[0] pick can attribute the wrong copy to a
    given source mailbox. The poller's forward path always rewrites the
    From header to the SOURCE mailbox's address (see
    purelymail-notify-poller.build_forward_message()), so each forwarded
    copy's Graph ``from`` reliably identifies which source it came from:

    * No results -> ("not_found", None) -- unchanged, nothing to disambiguate.
    * Exactly one result -> its ``from`` address MUST equal
      ``source_mailbox`` (case-insensitive). If it matches, use it. If it
      does not match (or the From address is missing/malformed), that
      single copy is NOT this record's forwarded copy -- a real message
      exists in the mailbox but it isn't attributable to this source, so
      this is treated as a no-match -> ("unknown", None), with a stderr
      warning. (This is distinct from "not_found": a message under that
      Message-ID does exist, just not the one we're looking for.)
    * More than one result -> filter to results whose ``from`` address
      equals ``source_mailbox`` (case-insensitive). If exactly one
      candidate matches, use it. If zero or more than one match, the
      attribution is ambiguous -> ("unknown", None), with a stderr warning.
    """
    mailbox_quoted = urllib.parse.quote(destination, safe="")
    filter_expr = f"internetMessageId eq '{_odata_escape(message_id)}'"
    query = urllib.parse.urlencode(
        {"$filter": filter_expr, "$select": "id,parentFolderId,from", "$top": "5"},
        quote_via=urllib.parse.quote,
    )
    url = f"{GRAPH_ROOT}/users/{mailbox_quoted}/messages?{query}"
    payload = graph_get(fetcher, url, token, sleeper=sleeper)
    values = payload.get("value")
    if not isinstance(values, list):
        raise CollectorError("Microsoft Graph messages response was malformed")
    if not values:
        return "not_found", None

    normalized_source = source_mailbox.strip().lower()

    if len(values) == 1:
        candidate = values[0]
        if _from_address(candidate) != normalized_source:
            print(
                f"warning: destination mailbox message for internetMessageId "
                f"{message_id!r} has a From address that does not match source "
                f"mailbox {source_mailbox!r}; not attributing it -- "
                "m365_placement=unknown",
                file=sys.stderr,
            )
            return "unknown", None
        chosen = candidate
    else:
        matches = [item for item in values if _from_address(item) == normalized_source]
        if len(matches) != 1:
            print(
                f"warning: {len(values)} destination mailbox messages found for "
                f"internetMessageId {message_id!r} and {len(matches)} have a From "
                f"address matching source mailbox {source_mailbox!r} (need exactly "
                "1 to disambiguate) -- m365_placement=unknown",
                file=sys.stderr,
            )
            return "unknown", None
        chosen = matches[0]

    parent_id = chosen.get("parentFolderId") if isinstance(chosen, dict) else None
    if not isinstance(parent_id, str) or not parent_id:
        return "other", None

    info = folder_map.get(parent_id)
    if info is None:
        # Folder id could not be resolved in the mailbox's folder tree (e.g.
        # a special/system folder not returned by mailFolders); the message
        # itself was found, so this is "other" rather than "not_found".
        return "other", parent_id
    if info.well_known_name in WELL_KNOWN_TO_PLACEMENT:
        return WELL_KNOWN_TO_PLACEMENT[info.well_known_name], info.well_known_name
    return "other", info.display_name or info.well_known_name or parent_id


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    raw = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CollectorError(f"invalid JSON in {path.name}:{line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise CollectorError(f"record in {path.name}:{line_number} must be an object")
        records.append(value)
    return records


def _valid_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maximum:
        return None
    return text


def write_atomic(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def collect_placements(
    fetcher: Fetcher,
    *,
    labelled: list[dict[str, Any]],
    destination: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    limit: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[list[dict[str, Any]], Counter]:
    """Core, network-facing collection loop (fetcher is fully injectable)."""
    token = get_access_token(
        fetcher, tenant_id=tenant_id, client_id=client_id, client_secret=client_secret,
        sleeper=sleeper,
    )
    folder_map = build_folder_map(fetcher, token, destination, sleeper=sleeper)

    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    skipped_invalid = 0
    skipped_duplicate = 0
    for record in labelled:
        record_id = _valid_text(record.get("id"), maximum=MAX_ID_LENGTH)
        mailbox = _valid_text(record.get("mailbox"), maximum=MAX_MAILBOX_LENGTH)
        message_id = _valid_text(record.get("message_id"), maximum=MAX_MESSAGE_ID_LENGTH)
        if record_id is None or mailbox is None or message_id is None:
            skipped_invalid += 1
            continue
        if record_id in seen_ids:
            skipped_duplicate += 1
            continue
        seen_ids.add(record_id)
        deduped.append({"id": record_id, "mailbox": mailbox, "message_id": message_id})

    if limit is not None:
        deduped = deduped[:limit]

    placement_counts: Counter = Counter()
    output: list[dict[str, Any]] = []
    for index, record in enumerate(deduped):
        if index > 0:
            sleeper(REQUEST_DELAY_SECONDS)
        placement, folder = find_placement(
            fetcher, token, destination, record["message_id"], folder_map,
            source_mailbox=record["mailbox"], sleeper=sleeper,
        )
        placement_counts[placement] += 1
        output.append(
            {
                "id": record["id"],
                "mailbox": record["mailbox"],
                "m365_placement": placement,
                # Deliberately NOT "observed_at" (or any of its evaluator
                # aliases "timestamp"/"received_at"): evaluate-spam.py's
                # detect_same_key_semantic_conflicts() treats those as a
                # shared semantic field across the labelled+placement join,
                # and this probe's wall-clock time will always differ from
                # collect-labels.py's IMAP internaldate observed_at, which
                # would flag conflicting_semantic_field_observed_at on every
                # joined record. "probed_at" is a non-semantic extra field
                # the evaluator ignores.
                "probed_at": clock().astimezone(timezone.utc).isoformat(timespec="seconds"),
                "m365_folder": folder,
                "match_key": "internet_message_id",
            }
        )

    output.sort(key=lambda item: (item["mailbox"], item["id"]))
    stats = Counter(placement_counts)
    stats["_input_records"] = len(labelled)
    stats["_skipped_invalid"] = skipped_invalid
    stats["_skipped_duplicate_id"] = skipped_duplicate
    stats["_output_records"] = len(output)
    return output, stats


def resolve_destination(explicit: str | None, config_path: Path) -> str:
    if explicit:
        return explicit
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectorError(f"could not read config file {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CollectorError(f"config file {config_path} is not valid JSON: {exc}") from exc
    forward_to = config.get("forward_to") if isinstance(config, dict) else None
    if not isinstance(forward_to, str) or not forward_to.strip():
        raise CollectorError(f"config file {config_path} has no usable 'forward_to' mailbox")
    return forward_to.strip()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe the M365 destination mailbox via Microsoft Graph and produce "
            "the placements JSONL consumed by evaluate-spam.py (join key: id). "
            "Requires app-only client-credentials in the environment: "
            "M365_PLACEMENT_TENANT_ID, M365_PLACEMENT_CLIENT_ID, "
            "M365_PLACEMENT_CLIENT_SECRET."
        )
    )
    parser.add_argument("--labelled", type=Path, required=True, help="labelled draft JSONL input")
    parser.add_argument("--output", type=Path, required=True, help="placements JSONL output path")
    parser.add_argument(
        "--destination",
        help="M365 mailbox to probe (default: 'forward_to' from --config)",
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"runtime config, read-only, only for 'forward_to' (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--secrets-file", type=Path, help="optional KEY=value env file")
    parser.add_argument("--limit", type=int, help="process at most this many labelled records")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit cannot be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.secrets_file:
        load_secrets_file(args.secrets_file)

    tenant_id = os.environ.get("M365_PLACEMENT_TENANT_ID")
    client_id = os.environ.get("M365_PLACEMENT_CLIENT_ID")
    client_secret = os.environ.get("M365_PLACEMENT_CLIENT_SECRET")
    missing = [
        name
        for name, value in (
            ("M365_PLACEMENT_TENANT_ID", tenant_id),
            ("M365_PLACEMENT_CLIENT_ID", client_id),
            ("M365_PLACEMENT_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        print(
            f"collect-m365-placements: missing required credential env var(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    try:
        destination = resolve_destination(args.destination, args.config)
        labelled = read_jsonl(args.labelled)
        fetcher: Fetcher = UrllibFetcher()
        output, stats = collect_placements(
            fetcher,
            labelled=labelled,
            destination=destination,
            tenant_id=tenant_id,  # type: ignore[arg-type]
            client_id=client_id,  # type: ignore[arg-type]
            client_secret=client_secret,  # type: ignore[arg-type]
            limit=args.limit,
        )
        lines = [json.dumps(record, sort_keys=True) for record in output]
        write_atomic(args.output, lines)
    except CollectorError as exc:
        print(f"collect-m365-placements: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"collect-m365-placements: I/O error: {exc}", file=sys.stderr)
        return 2

    print(
        "collect-m365-placements: destination={dest} input={inp} output={out} "
        "skipped_invalid={inv} skipped_duplicate_id={dup} placements={placements}".format(
            dest=destination,
            inp=stats["_input_records"],
            out=stats["_output_records"],
            inv=stats["_skipped_invalid"],
            dup=stats["_skipped_duplicate_id"],
            placements=dict(
                sorted(
                    (k, v) for k, v in stats.items() if not str(k).startswith("_")
                )
            ),
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
