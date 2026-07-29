#!/usr/bin/env python3
"""Merge a DRAFT labelled snapshot into a cumulative labelled corpus.

Standalone, stdlib-only, read/write only against local JSONL files -- no
network, no credentials. PurelyMail appears to purge mail after roughly
5-7 days, so a single ``collect-labels.py`` snapshot is a narrow, decaying
window of evidence. This tool accrues that evidence over time: each run
merges a fresh DRAFT snapshot (see ``collect-labels.py``) into a durable
corpus JSONL, keyed by each record's ``id``, so the corpus keeps evidence
for messages long after PurelyMail has purged them.

Merge semantics (see ``merge_corpus`` for the authoritative logic):

* A snapshot ``id`` not already in the corpus is appended as-is.
* A snapshot ``id`` already in the corpus is normally left untouched --
  the existing record is treated as the corpus's record of truth -- except
  in exactly two upgrade cases:

  1. The existing record has no ``prediction`` (key absent or null) and the
     snapshot record has one: the corpus record adopts the snapshot's
     ``prediction`` (fills in previously-unavailable prediction evidence).
  2. The existing record's ``purelymail_placement`` is ``"inbox"`` and the
     snapshot record's is ``"junk"``: the user moved the message to Junk
     after it was originally observed in the inbox. This is a *stronger*
     spam signal than the original placement, so the corpus record is
     replaced wholesale with the snapshot record (adopting the snapshot's
     label/placement/prediction/etc.), except ``observed_at`` is kept at
     its original value, and a ``reclassified_to_junk: true`` marker is
     added so downstream consumers can see this happened.

Corpus lines (and, defensively, snapshot lines) that are not valid JSON
objects, are missing a usable ``id``/``mailbox``/``observed_at``, or
duplicate an ``id`` already seen in the same file are treated as corpus
corruption: this tool fails closed (exit code 2) and never writes anything,
so a corrupted corpus is never silently accepted or overwritten.

The corpus is written atomically (temp file, ``fsync``, ``os.replace``) in
deterministic ``(mailbox, observed_at, id)`` order, and a one-line summary
of added/updated/reclassified/pruned/total counts is printed to stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class CorpusError(RuntimeError):
    """Any invalid/corrupt input -- fail closed, exit 2, no write."""


# --------------------------------------------------------------------------
# Small stdlib helpers
# --------------------------------------------------------------------------


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
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


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Loading / validation
# --------------------------------------------------------------------------


def _validate_record(raw: Any, *, source: str, lineno: int) -> dict[str, Any]:
    """Validate one parsed JSONL line as a usable labelled record.

    Requires ``id``, ``mailbox``, and ``observed_at`` to be non-empty
    strings, and ``observed_at`` to be a parseable timezone-aware
    ISO-8601 timestamp. Anything else raises CorpusError -- this is the
    fail-closed gate described in the module docstring.
    """
    if not isinstance(raw, dict):
        raise CorpusError(f"{source} line {lineno}: not a JSON object")
    record_id = raw.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise CorpusError(f"{source} line {lineno}: missing or invalid id")
    mailbox = raw.get("mailbox")
    if not isinstance(mailbox, str) or not mailbox.strip():
        raise CorpusError(f"{source} line {lineno}: record {record_id!r} missing or invalid mailbox")
    observed_at = raw.get("observed_at")
    if parse_timestamp(observed_at) is None:
        raise CorpusError(
            f"{source} line {lineno}: record {record_id!r} has an invalid observed_at"
        )
    return raw


def load_jsonl_records(path: Path, *, source: str, allow_missing: bool) -> list[dict[str, Any]]:
    """Load and validate JSONL records, failing closed on any corruption.

    Duplicate ``id`` values within the same file are corruption: the file
    is expected to already be keyed by ``id`` (both the corpus this tool
    writes and the snapshot ``collect-labels.py`` writes are), so a repeat
    id means something upstream is broken and must not be trusted.
    """
    if not path.exists():
        if allow_missing:
            return []
        raise CorpusError(f"{source} file does not exist: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorpusError(f"could not read {source} file {path}: {exc}") from exc

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for lineno, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorpusError(f"{source} line {lineno}: invalid JSON ({exc})") from exc
        record = _validate_record(parsed, source=source, lineno=lineno)
        record_id = record["id"]
        if record_id in seen_ids:
            raise CorpusError(f"{source} line {lineno}: duplicate id {record_id!r}")
        seen_ids.add(record_id)
        records.append(record)
    return records


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


class MergeCounts:
    def __init__(self) -> None:
        self.added = 0
        self.updated = 0
        self.reclassified = 0
        self.pruned = 0


def _has_prediction(record: dict[str, Any]) -> bool:
    return record.get("prediction") is not None


def merge_corpus(
    corpus_records: list[dict[str, Any]], snapshot_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], MergeCounts]:
    """Merge ``snapshot_records`` into ``corpus_records``; see module docstring."""
    corpus_by_id = {record["id"]: dict(record) for record in corpus_records}
    counts = MergeCounts()

    for snapshot_record in snapshot_records:
        record_id = snapshot_record["id"]
        existing = corpus_by_id.get(record_id)
        if existing is None:
            corpus_by_id[record_id] = dict(snapshot_record)
            counts.added += 1
            continue

        # Case (b) takes precedence: a wholesale adoption already carries
        # whatever prediction the snapshot has, making a separate case-(a)
        # upgrade on the same record redundant.
        if existing.get("purelymail_placement") == "inbox" and snapshot_record.get("purelymail_placement") == "junk":
            original_observed_at = existing["observed_at"]
            reclassified_record = dict(snapshot_record)
            reclassified_record["observed_at"] = original_observed_at
            reclassified_record["reclassified_to_junk"] = True
            corpus_by_id[record_id] = reclassified_record
            counts.reclassified += 1
            continue

        if not _has_prediction(existing) and _has_prediction(snapshot_record):
            updated = dict(existing)
            updated["prediction"] = snapshot_record["prediction"]
            corpus_by_id[record_id] = updated
            counts.updated += 1
            continue

        # Neither upgrade applies -- keep the existing record untouched.

    merged = list(corpus_by_id.values())
    return merged, counts


def prune_corpus(
    records: list[dict[str, Any]], *, max_age_days: float, now: datetime,
) -> tuple[list[dict[str, Any]], int]:
    cutoff = now - timedelta(days=max_age_days)
    kept: list[dict[str, Any]] = []
    pruned = 0
    for record in records:
        observed_at = parse_timestamp(record.get("observed_at"))
        # Already validated as parseable at load time.
        assert observed_at is not None
        if observed_at < cutoff:
            pruned += 1
        else:
            kept.append(record)
    return kept, pruned


def sort_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (record["mailbox"], record["observed_at"], record["id"])


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_corpus(path: Path, records: list[dict[str, Any]]) -> None:
    """Atomically write ``records`` as sorted JSONL: temp file, fsync, rename.

    A failure at any point before ``os.replace`` never touches ``path``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def print_summary(counts: MergeCounts, total: int) -> None:
    print(
        f"summary: added={counts.added} updated={counts.updated} "
        f"reclassified={counts.reclassified} pruned={counts.pruned} total={total}",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="new draft JSONL from collect-labels.py")
    parser.add_argument("--corpus", type=Path, required=True, help="cumulative JSONL; created if missing")
    parser.add_argument(
        "--max-age-days", type=float, default=None,
        help="prune corpus records older than this by observed_at (default: no pruning)",
    )
    args = parser.parse_args(argv)

    if args.max_age_days is not None and (not math.isfinite(args.max_age_days) or args.max_age_days <= 0):
        parser.error("--max-age-days must be a positive finite number")

    return args


def run(args: argparse.Namespace) -> None:
    corpus_records = load_jsonl_records(args.corpus, source="corpus", allow_missing=True)
    snapshot_records = load_jsonl_records(args.snapshot, source="snapshot", allow_missing=False)

    merged, counts = merge_corpus(corpus_records, snapshot_records)

    if args.max_age_days is not None:
        merged, counts.pruned = prune_corpus(
            merged, max_age_days=args.max_age_days, now=datetime.now(timezone.utc),
        )

    merged.sort(key=sort_key)
    write_corpus(args.corpus, merged)
    print_summary(counts, len(merged))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except CorpusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
