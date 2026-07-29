#!/usr/bin/env python3
"""Collect a DRAFT labelled-corpus JSONL for poller/evaluate-spam.py.

Standalone, read-only, stdlib-only. For each selected Purelymail mailbox
this connects over TLS-verified IMAP (never marking messages ``\\Seen``,
never fetching bodies) and lists Message-ID/From/Subject/INTERNALDATE for
recent mail in ``INBOX`` and ``Junk``. The folder a message currently sits
in becomes a DRAFT label ("HAM" for INBOX, "SPAM" for Junk) -- this is a
heuristic proxy for ground truth, not curated ground truth itself. It is
read-only against the poller's own state files (``forwarded_message_ids``,
``quarantine_holds``) to additionally attach a ``prediction`` field so the
draft corpus can be joined straight into ``evaluate-spam.py``.

The output is emphatically a DRAFT: every run prints a loud notice that a
human must review/curate labels before the corpus is used to evaluate or
gate anything. This tool never mutates mailbox state, never writes to
IMAP, and never touches the poller's own state files (it only reads them).
"""

from __future__ import annotations

import argparse
import email
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Protocol


# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_purelymail_learning() -> ModuleType:
    """Load the sibling purelymail_learning.py by exact file path.

    Deliberately does NOT mutate sys.path: this script and its tests run
    inside the same process as the rest of the poller test suite, and
    inserting SCRIPT_DIR into sys.path would change which import branch
    purelymail-notify-poller.py's own try/except (plain vs `poller.`
    -qualified) takes for later-loaded modules, breaking exception-identity
    matching in unrelated tests (observed: test_graph_feedback_integration.py
    started failing once this file did `sys.path.insert(0, SCRIPT_DIR)`).
    Caches under sys.modules["purelymail_learning"] so repeated loads (e.g.
    across tests in one process) are cheap and stable.
    """
    module_name = "purelymail_learning"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_DIR / "purelymail_learning.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load purelymail_learning.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


VerifiedImapClient = _load_purelymail_learning().VerifiedImapClient

# Mirrors purelymail-notify-poller.py's STATE_DIR (HERMES_HOME / "state" /
# "purelymail-poller"); duplicated here rather than importing the hyphenated
# poller module, which this tool must not touch or depend on.
DEFAULT_STATE_DIR = Path.home() / ".hermes" / "state" / "purelymail-poller"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "notify-poller.config.json"

FOLDERS = ("INBOX", "Junk")
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
MAX_HEADER_CHARS = 500
INTERNALDATE_RE = re.compile(rb'INTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
INTERNALDATE_TEXT_RE = re.compile(
    r"(\d{2})-(\w{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})"
)
MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
MONTH_NUMBER = {name: index + 1 for index, name in enumerate(MONTH_ABBR)}


class CollectionError(RuntimeError):
    """Any config/auth/IMAP failure -- fail closed, exit 2, no partial output."""


# --------------------------------------------------------------------------
# IMAP client seam (matches purelymail_learning.ImapClient's shape plus the
# close()/logout() methods VerifiedImapClient also exposes)
# --------------------------------------------------------------------------


class ImapClientProtocol(Protocol):
    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[Any]]: ...
    def status(self, mailbox: str, names: str) -> tuple[str, list[Any]]: ...
    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...
    def close(self) -> None: ...
    def logout(self) -> None: ...


ClientFactory = Callable[[str, int, str, str], ImapClientProtocol]


def _default_client_factory(host: str, port: int, mailbox: str, password: str) -> ImapClientProtocol:
    return VerifiedImapClient(host, port, mailbox, password)


def _safe_close(client: ImapClientProtocol) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass
    try:
        client.logout()
    except Exception:  # noqa: BLE001 - best-effort cleanup only
        pass


# --------------------------------------------------------------------------
# Small stdlib helpers (secrets file format matches the poller's
# load_secrets_file; duplicated rather than imported since that lives in the
# hyphenated poller module this tool must not depend on)
# --------------------------------------------------------------------------


def load_secrets_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"warning: could not read secrets file {path}: {exc}", file=sys.stderr)
        return
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"warning: ignoring malformed line {lineno} in secrets file {path}", file=sys.stderr)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not key:
            print(f"warning: ignoring malformed line {lineno} in secrets file {path}", file=sys.stderr)
            continue
        if key in os.environ:
            continue
        os.environ[key] = value


def _sanitize_state_filename(address: str) -> str:
    """Mirror purelymail-notify-poller.py's sanitize_address() exactly."""
    return address.replace("@", "_at_").replace(".", "_").replace("/", "_")


def _sanitize_header_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    text = CONTROL_CHARACTERS.sub(" ", text).strip()
    return text[:MAX_HEADER_CHARS]


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# IMAP wire parsing
# --------------------------------------------------------------------------


def _imap_since_date(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    return f"{utc.day:02d}-{MONTH_ABBR[utc.month - 1]}-{utc.year:04d}"


def _parse_internaldate(text: str) -> datetime:
    match = INTERNALDATE_TEXT_RE.fullmatch(text.strip())
    if not match:
        raise CollectionError(f"unparseable IMAP INTERNALDATE: {text!r}")
    day, month_abbr, year, hour, minute, second, tz = match.groups()
    month = MONTH_NUMBER.get(month_abbr)
    if month is None:
        raise CollectionError(f"unparseable IMAP INTERNALDATE month: {text!r}")
    sign = 1 if tz[0] == "+" else -1
    offset = timedelta(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
    naive = datetime(int(year), month, int(day), int(hour), int(minute), int(second))
    return naive.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)


def _parse_uid_list(search_data: list[Any]) -> list[int]:
    uids: set[int] = set()
    for chunk in search_data:
        if isinstance(chunk, bytes):
            uids.update(int(token) for token in chunk.split() if token.isdigit())
    return sorted(uids)


def _parse_fetch_response(fetch_data: list[Any]) -> tuple[datetime, bytes]:
    internaldate_raw: bytes | None = None
    header_bytes = b""
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2:
            meta, chunk = item[0], item[1]
            if isinstance(meta, bytes):
                match = INTERNALDATE_RE.search(meta)
                if match:
                    internaldate_raw = match.group(1)
            if isinstance(chunk, bytes):
                header_bytes += chunk
        elif isinstance(item, bytes):
            match = INTERNALDATE_RE.search(item)
            if match:
                internaldate_raw = match.group(1)
    if internaldate_raw is None:
        raise CollectionError("IMAP FETCH response did not include INTERNALDATE")
    internaldate = _parse_internaldate(internaldate_raw.decode("ascii", "replace"))
    return internaldate, header_bytes


def _message_id_header(parsed: email.message.Message) -> str | None:
    values = parsed.get_all("Message-ID", [])
    if not values:
        return None
    value = str(values[0]).strip()
    return value or None


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HeaderInfo:
    internaldate: datetime
    from_header: str
    subject_header: str


@dataclass(frozen=True)
class DraftRecord:
    id: str
    mailbox: str
    label: str
    label_source: str
    purelymail_placement: str
    observed_at: datetime
    message_id: str
    from_header: str
    subject: str
    prediction: str | None = None
    conflict: bool = False

    def sort_key(self) -> tuple[str, str, str]:
        return (self.mailbox, _iso_z(self.observed_at), self.id)

    def to_json_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "mailbox": self.mailbox,
            "label": self.label,
            "label_source": self.label_source,
            "purelymail_placement": self.purelymail_placement,
            "observed_at": _iso_z(self.observed_at),
            "message_id": self.message_id,
            "from": self.from_header,
            "subject": self.subject,
        }
        if self.prediction is not None:
            result["prediction"] = self.prediction
        if self.conflict:
            result["conflict"] = True
        return result


@dataclass(frozen=True)
class CollectionOutcome:
    records: list[DraftRecord]
    summary: dict[str, dict[str, int]]


# --------------------------------------------------------------------------
# Prediction join against poller state (read-only)
# --------------------------------------------------------------------------


def _load_state_for_prediction(state_dir: Path, address: str) -> dict[str, Any] | None:
    path = state_dir / f"{_sanitize_state_filename(address)}.json"
    if not path.exists():
        print(
            f"warning: no poller state file for {address} at {path}; "
            "predictions will be omitted for this mailbox",
            file=sys.stderr,
        )
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state root must be an object")
        if not isinstance(data.get("forwarded_message_ids", []), list):
            raise ValueError("forwarded_message_ids must be a list")
        if not isinstance(data.get("quarantine_holds", {}), dict):
            raise ValueError("quarantine_holds must be an object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(
            f"warning: could not read poller state for {address} ({exc}); "
            "predictions will be omitted for this mailbox",
            file=sys.stderr,
        )
        return None
    return data


def _prediction_indexes(state: dict[str, Any] | None) -> tuple[set[str], set[str]]:
    """Return (forwarded -> HAM, held -> SPAM) exact Message-ID sets.

    NOTE (spec deviation): the task description referred to
    ``quarantine_holds[*].classification.message_id``, but the poller's
    actual state schema (see purelymail-notify-poller.py's
    ``_record_quarantine_hold``) stores ``message_id`` directly on each
    hold record, not nested under a ``classification`` key. This reads the
    real field.

    NOTE (precedence): a message_id can appear in both sets (a held message
    that was later replayed/released and forwarded). The join in
    ``collect_labels`` resolves that overlap to SPAM, not HAM: the hold's
    classifier verdict was SPAM and does not retroactively become correct
    just because the message was later forwarded, so these must still count
    as false positives against the ham-safety bound.
    """
    if state is None:
        return set(), set()
    forwarded_ids = {item for item in state.get("forwarded_message_ids", []) if isinstance(item, str)}
    held_ids: set[str] = set()
    for hold in state.get("quarantine_holds", {}).values():
        if isinstance(hold, dict):
            message_id = hold.get("message_id")
            if isinstance(message_id, str):
                held_ids.add(message_id)
    return forwarded_ids, held_ids


# --------------------------------------------------------------------------
# IMAP collection
# --------------------------------------------------------------------------


def _collect_folder(
    client: ImapClientProtocol,
    folder: str,
    since_date: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, HeaderInfo]:
    status, _ = client.select(folder, readonly=True)
    if status != "OK":
        raise CollectionError(f"could not select folder {folder!r}: status {status}")

    status, search_data = client.uid("search", None, "SINCE", since_date)
    if status != "OK":
        raise CollectionError(f"UID SEARCH failed in {folder!r}: status {status}")
    uids = _parse_uid_list(search_data)

    result: dict[str, HeaderInfo] = {}
    for uid in uids:
        status, fetch_data = client.uid(
            "fetch", str(uid), "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT)])",
        )
        if status != "OK":
            raise CollectionError(f"UID FETCH failed for UID {uid} in {folder!r}: status {status}")
        internaldate, header_bytes = _parse_fetch_response(fetch_data)
        if not (window_start <= internaldate <= window_end):
            continue
        parsed = email.message_from_bytes(header_bytes)
        message_id = _message_id_header(parsed)
        if not message_id:
            print(
                f"warning: skipping UID {uid} in {folder!r} -- missing/empty Message-ID header",
                file=sys.stderr,
            )
            continue
        result[message_id] = HeaderInfo(
            internaldate=internaldate,
            from_header=_sanitize_header_text(parsed.get("From")),
            subject_header=_sanitize_header_text(parsed.get("Subject")),
        )
    return result


def collect_labels(
    *,
    config: dict[str, Any],
    mailbox_configs: list[dict[str, Any]],
    state_dir: Path,
    window_start: datetime,
    window_end: datetime,
    client_factory: ClientFactory = _default_client_factory,
) -> CollectionOutcome:
    imap_cfg = config.get("imap", {})
    host = imap_cfg.get("host")
    port = imap_cfg.get("port")
    if not isinstance(host, str) or not host.strip():
        raise CollectionError("config imap.host is invalid")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise CollectionError("config imap.port is invalid")

    # IMAP SINCE is date-only and server-timezone dependent, so it cannot be
    # trusted as a precise lower bound. Buffer it one full day earlier than
    # window_start and rely on the client-side INTERNALDATE check in
    # _collect_folder (which filters both window_start and window_end) for
    # the precise cutoff.
    since_date = _imap_since_date(window_start - timedelta(days=1))
    records: list[DraftRecord] = []
    summary: dict[str, dict[str, int]] = {}

    for mailbox_cfg in mailbox_configs:
        address = mailbox_cfg["address"]
        mailbox = address.strip().lower()
        secret_env = mailbox_cfg["secret_env"]
        password = os.environ.get(secret_env)
        if not password:
            raise CollectionError(
                f"missing password for mailbox {address}: env var {secret_env} is not set"
            )

        try:
            client = client_factory(host, port, address, password)
        except Exception as exc:  # noqa: BLE001 - any connect/auth failure is fatal here
            raise CollectionError(f"IMAP connect/auth failed for {address}: {exc}") from exc

        try:
            inbox_messages = _collect_folder(client, "INBOX", since_date, window_start, window_end)
            junk_messages = _collect_folder(client, "Junk", since_date, window_start, window_end)
        finally:
            _safe_close(client)

        state = _load_state_for_prediction(state_dir, address)
        forwarded_ids, held_ids = _prediction_indexes(state)

        predicted_count = 0
        for message_id in sorted(set(inbox_messages) | set(junk_messages)):
            in_junk = message_id in junk_messages
            in_inbox = message_id in inbox_messages
            if in_junk:
                label = "SPAM"
                placement = "junk"
                header = junk_messages[message_id]
                conflict = in_inbox
            else:
                label = "HAM"
                placement = "inbox"
                header = inbox_messages[message_id]
                conflict = False

            prediction: str | None = None
            # Precedence: a quarantine hold's classifier verdict is SPAM by
            # definition, and that verdict does not change if the held
            # message is later replayed/released and forwarded. A message_id
            # that matches BOTH quarantine_holds and forwarded_message_ids is
            # exactly the false-positive case the evaluator must count
            # against the ham-safety bound -- letting forwarded state mask
            # it as HAM would hide FPs from the metric. So held_ids (SPAM)
            # takes precedence over forwarded_ids (HAM).
            if message_id in held_ids:
                prediction = "SPAM"
            elif message_id in forwarded_ids:
                prediction = "HAM"
            if prediction is not None:
                predicted_count += 1

            record_id = hashlib.sha256(f"{mailbox}\0{message_id}".encode("utf-8")).hexdigest()
            records.append(
                DraftRecord(
                    id=record_id,
                    mailbox=mailbox,
                    label=label,
                    label_source="draft:purelymail_folder",
                    purelymail_placement=placement,
                    observed_at=header.internaldate,
                    message_id=message_id,
                    from_header=header.from_header,
                    subject=header.subject_header,
                    prediction=prediction,
                    conflict=conflict,
                )
            )

        summary[mailbox] = {
            "inbox": len(inbox_messages),
            "junk": len(junk_messages),
            "predicted": predicted_count,
        }

    records.sort(key=lambda record: record.sort_key())
    return CollectionOutcome(records=records, summary=summary)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_records(output_path: Path, records: list[DraftRecord]) -> None:
    """Write records as sorted JSONL; temp-then-rename so a failure never
    leaves a partial file at ``output_path``."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record.to_json_dict(), sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, output_path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _warn_if_partial_mailbox_selection(selected_count: int, total_configured: int) -> None:
    """Warn loudly when --mailbox narrows collection to a strict subset.

    evaluate-spam.py's runtime re-verifier (purelymail-notify-poller.py)
    requires an artifact's mailbox coverage to exactly match ALL configured
    (+ canary) mailboxes, byte-for-byte. A corpus collected from a narrowed
    --mailbox selection can never satisfy that -- it is for spot-checking a
    mailbox in isolation only, never for producing a bindable artifact.
    """
    banner = "=" * 72
    print(banner, file=sys.stderr)
    print(
        f"WARNING: --mailbox restricted collection to {selected_count} of "
        f"{total_configured} configured mailboxes.",
        file=sys.stderr,
    )
    print(
        "The runtime activation gate requires an evaluate-spam.py artifact "
        "whose mailbox coverage exactly matches ALL configured mailboxes "
        "(plus any canary mailboxes) -- a corpus built from this narrowed "
        "selection CANNOT produce a bindable artifact. This output is for "
        "spot-checking a mailbox in isolation only.",
        file=sys.stderr,
    )
    print(banner, file=sys.stderr)


def print_summary(summary: dict[str, dict[str, int]], output_path: Path) -> None:
    banner = "=" * 72
    print(banner, file=sys.stderr)
    print("DRAFT LABELS ONLY.", file=sys.stderr)
    print(
        "Purelymail folder placement (INBOX/Junk) is a heuristic proxy, "
        "NOT curated ground truth.",
        file=sys.stderr,
    )
    print(
        f"Human review/curation of {output_path} is REQUIRED before it (or "
        "any derivative) is used as evaluate-spam.py input.",
        file=sys.stderr,
    )
    print(banner, file=sys.stderr)
    for mailbox in sorted(summary):
        counts = summary[mailbox]
        print(
            f"[{mailbox}] inbox={counts['inbox']} junk={counts['junk']} "
            f"predicted={counts['predicted']}",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--secrets-file", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--window-days", type=float, default=30.0)
    parser.add_argument(
        "--mailbox", action="append", default=None,
        help="restrict collection to this mailbox address; repeatable (default: all configured)",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", default=None, help="tz-aware ISO-8601 timestamp (default: now, UTC)")
    args = parser.parse_args(argv)

    if not math.isfinite(args.window_days) or args.window_days <= 0:
        parser.error("--window-days must be a positive finite number")

    if args.as_of is None:
        args.as_of = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        parsed = parse_timestamp(args.as_of)
        if parsed is None:
            parser.error("--as-of must be a timezone-aware ISO-8601 timestamp")
        args.as_of = parsed

    return args


def _load_config(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CollectionError(f"could not read config {path}: {exc}") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectionError(f"invalid JSON in config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise CollectionError("config root must be an object")
    return config


def _select_mailboxes(
    config: dict[str, Any], requested: list[str] | None,
) -> list[dict[str, Any]]:
    all_mailboxes = config.get("mailboxes")
    if not isinstance(all_mailboxes, list) or not all_mailboxes:
        raise CollectionError("config.mailboxes must be a non-empty array")

    by_address: dict[str, dict[str, Any]] = {}
    for entry in all_mailboxes:
        if not isinstance(entry, dict):
            raise CollectionError("each config mailbox entry must be an object")
        address = entry.get("address")
        secret_env = entry.get("secret_env")
        if not isinstance(address, str) or "@" not in address or not address.strip():
            raise CollectionError("each config mailbox entry must have a valid address")
        if not isinstance(secret_env, str) or not secret_env.strip():
            raise CollectionError(f"mailbox {address} is missing secret_env")
        key = address.strip().lower()
        if key in by_address:
            raise CollectionError(f"duplicate mailbox address in config: {address}")
        by_address[key] = entry

    if not requested:
        return list(by_address.values())

    selected: list[dict[str, Any]] = []
    for raw_address in requested:
        key = raw_address.strip().lower()
        if key not in by_address:
            raise CollectionError(f"requested --mailbox is not configured: {raw_address}")
        selected.append(by_address[key])
    return selected


def run(args: argparse.Namespace) -> None:
    if args.secrets_file is not None:
        load_secrets_file(args.secrets_file)

    config = _load_config(args.config)
    selected_mailboxes = _select_mailboxes(config, args.mailbox)
    if args.mailbox:
        total_configured = len(config.get("mailboxes") or [])
        if len(selected_mailboxes) < total_configured:
            _warn_if_partial_mailbox_selection(len(selected_mailboxes), total_configured)

    window_end = args.as_of
    window_start = window_end - timedelta(days=args.window_days)

    outcome = collect_labels(
        config=config,
        mailbox_configs=selected_mailboxes,
        state_dir=args.state_dir,
        window_start=window_start,
        window_end=window_end,
        client_factory=_default_client_factory,
    )
    write_records(args.output, outcome.records)
    print_summary(outcome.summary, args.output)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except CollectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
