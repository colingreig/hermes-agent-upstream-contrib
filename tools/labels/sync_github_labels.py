#!/usr/bin/env python3
"""Render the canonical tools/labels/labels.json registry onto one or more
GitHub repos' label sets, via `gh api` (auth already configured elsewhere).

Python 3 stdlib only. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LABELS_PATH = os.path.join(SCRIPT_DIR, "labels.json")
RECEIPTS_DIR = os.path.join(SCRIPT_DIR, "receipts")

DESC_MAX = 100

# GitHub's built-in default labels, always skipped by --prune.
GITHUB_DEFAULT_LABELS = {
    "bug", "documentation", "duplicate", "enhancement",
    "good first issue", "help wanted", "invalid", "question", "wontfix",
}


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


class FatalError(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# gh CLI wrapper
# --------------------------------------------------------------------------

def check_gh_available():
    if shutil.which("gh") is None:
        raise FatalError("`gh` CLI not found on PATH. Install/authenticate GitHub CLI first.")
    try:
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL)
    except Exception as e:
        raise FatalError(f"Failed to run `gh auth status`: {e}")
    if result.returncode != 0:
        raise FatalError(
            f"`gh` is not authenticated. Run `gh auth login` first.\n{result.stdout}\n{result.stderr}")


def gh_api(args, call_log, verbose=False, input_data=None):
    """Run `gh api <args...>` and return parsed JSON (or None). Raises
    FatalError on a non-zero exit that isn't an expected 404."""
    cmd = ["gh", "api"] + args
    if verbose:
        eprint(f"[gh] {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                 input=input_data)
    except subprocess.TimeoutExpired:
        raise FatalError(f"gh api timed out: {' '.join(cmd)}")
    call_log.append({"cmd": cmd, "returncode": result.returncode})
    if result.returncode != 0:
        return result.returncode, result.stdout, result.stderr
    stdout = result.stdout.strip()
    parsed = json.loads(stdout) if stdout else None
    return 0, parsed, result.stderr


# --------------------------------------------------------------------------
# Registry helpers
# --------------------------------------------------------------------------

def load_labels():
    with open(LABELS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def registry_labels(labels: dict):
    """Yield (name, color_no_hash, description<=100) for every namespace:value."""
    out = []
    for ns, nsdef in labels["namespaces"].items():
        for val, valdef in nsdef["values"].items():
            name = f"{ns}:{val}"
            color = (valdef.get("color") or nsdef.get("color") or "#607d8b").lstrip("#")
            desc = valdef.get("desc", "")
            if len(desc) > DESC_MAX:
                desc = desc[: DESC_MAX - 1].rstrip() + "…"
            out.append((name, color, desc))
    return out


# --------------------------------------------------------------------------
# Repo sync
# --------------------------------------------------------------------------

def fetch_repo_labels(repo: str, call_log, verbose):
    labels = []
    page = 1
    while True:
        rc, parsed, stderr = gh_api(
            [f"repos/{repo}/labels", "-X", "GET", "-f", "per_page=100", "-f", f"page={page}"],
            call_log, verbose)
        if rc != 0:
            raise FatalError(f"Failed to list labels for {repo}: {stderr}")
        if not parsed:
            break
        labels.extend(parsed)
        if len(parsed) < 100:
            break
        page += 1
    return labels


def sync_repo(repo: str, target_labels, prune: bool, apply: bool, call_log, verbose, receipt):
    live = fetch_repo_labels(repo, call_log, verbose)
    live_by_name = {l["name"]: l for l in live}
    target_by_name = {name: (color, desc) for name, color, desc in target_labels}

    created, updated, unchanged, would_prune = [], [], [], []

    for name, color, desc in target_labels:
        existing = live_by_name.get(name)
        if existing is None:
            created.append(name)
            if apply:
                rc, parsed, stderr = gh_api(
                    [f"repos/{repo}/labels", "-X", "POST",
                     "-f", f"name={name}", "-f", f"color={color}", "-f", f"description={desc}"],
                    call_log, verbose)
                if rc != 0:
                    receipt["errors"].append(f"{repo}: failed to create '{name}': {stderr}")
                else:
                    receipt["writes"].append({"repo": repo, "op": "create", "label": name,
                                                "color": color, "description": desc})
        else:
            drift = existing.get("color", "").lower() != color.lower() or \
                (existing.get("description") or "") != desc
            if drift:
                updated.append(name)
                if apply:
                    rc, parsed, stderr = gh_api(
                        [f"repos/{repo}/labels/{name}", "-X", "PATCH",
                         "-f", f"new_name={name}", "-f", f"color={color}", "-f", f"description={desc}"],
                        call_log, verbose)
                    if rc != 0:
                        receipt["errors"].append(f"{repo}: failed to update '{name}': {stderr}")
                    else:
                        receipt["writes"].append({"repo": repo, "op": "update", "label": name,
                                                    "color": color, "description": desc,
                                                    "was_color": existing.get("color"),
                                                    "was_description": existing.get("description")})
            else:
                unchanged.append(name)

    if prune:
        for name in live_by_name:
            if name in target_by_name:
                continue
            if name in GITHUB_DEFAULT_LABELS:
                continue
            would_prune.append(name)
            if apply:
                rc, parsed, stderr = gh_api(
                    [f"repos/{repo}/labels/{name}", "-X", "DELETE"], call_log, verbose)
                if rc != 0:
                    receipt["errors"].append(f"{repo}: failed to delete '{name}': {stderr}")
                else:
                    receipt["writes"].append({"repo": repo, "op": "delete", "label": name})

    skipped_defaults = sorted(
        n for n in live_by_name if n in GITHUB_DEFAULT_LABELS and n not in target_by_name
    ) if prune else []

    return {
        "repo": repo,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "would_prune": would_prune,
        "skipped_defaults": skipped_defaults,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Render tools/labels/labels.json onto GitHub repo label sets.")
    p.add_argument("--repo", action="append", default=[], required=True,
                    help="owner/name (repeatable, required).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Show what would change (default).")
    mode.add_argument("--apply", action="store_true", help="Actually create/update/prune labels.")
    p.add_argument("--prune", action="store_true",
                    help="Delete repo labels absent from the registry (skips GitHub defaults).")
    p.add_argument("--receipt", default=None, help="Receipt output path.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    try:
        check_gh_available()
    except FatalError as e:
        eprint(f"ERROR: {e}")
        return 2

    try:
        labels = load_labels()
    except Exception as e:
        eprint(f"ERROR: failed to load {LABELS_PATH}: {e}")
        return 2

    target_labels = registry_labels(labels)
    labels_sha = sha256_of_file(LABELS_PATH)

    apply = bool(args.apply)
    mode = "apply" if apply else "dry_run"

    call_log = []
    receipt = {
        "mode": mode,
        "utc_timestamp": datetime.now(timezone.utc).isoformat(),
        "labels_json_sha256": labels_sha,
        "dry_run": not apply,
        "repos": args.repo,
        "prune": args.prune,
        "writes": [],
        "errors": [],
        "per_repo_summary": [],
    }

    receipt_path = args.receipt or os.path.join(RECEIPTS_DIR, f"github-{utc_now_iso()}.json")

    def flush_receipt():
        receipt["gh_api_calls_made"] = len(call_log)
        os.makedirs(os.path.dirname(os.path.abspath(receipt_path)), exist_ok=True)
        with open(receipt_path, "w", encoding="utf-8") as f:
            json.dump(receipt, f, indent=2, default=str)
        eprint(f"\nReceipt written: {receipt_path}")

    print("=" * 72)
    print(f"GITHUB LABEL SYNC ({'APPLY' if apply else 'DRY RUN'})")
    print("=" * 72)
    print(f"registry: {len(target_labels)} labels from labels.json")
    print(f"repos: {', '.join(args.repo)}")
    print(f"prune: {args.prune}")
    print()

    try:
        for repo in args.repo:
            summary = sync_repo(repo, target_labels, args.prune, apply, call_log, args.verbose, receipt)
            receipt["per_repo_summary"].append(summary)

            print(f"--- {repo} ---")
            print(f"  would create: {len(summary['created'])}")
            for n in summary["created"]:
                print(f"    + {n}")
            print(f"  would update: {len(summary['updated'])}")
            for n in summary["updated"]:
                print(f"    ~ {n}")
            print(f"  unchanged:    {len(summary['unchanged'])}")
            if args.prune:
                print(f"  would prune:  {len(summary['would_prune'])}")
                for n in summary["would_prune"]:
                    print(f"    - {n}")
                if summary["skipped_defaults"]:
                    print(f"  skipped GitHub defaults (never pruned): {', '.join(summary['skipped_defaults'])}")
            print()

        print("=" * 72)
        print("SUMMARY (all repos)")
        print("=" * 72)
        total_created = sum(len(s["created"]) for s in receipt["per_repo_summary"])
        total_updated = sum(len(s["updated"]) for s in receipt["per_repo_summary"])
        total_unchanged = sum(len(s["unchanged"]) for s in receipt["per_repo_summary"])
        total_prune = sum(len(s["would_prune"]) for s in receipt["per_repo_summary"])
        verb = "created" if apply else "would create"
        verb_u = "updated" if apply else "would update"
        verb_p = "pruned" if apply else "would prune"
        print(f"{verb}: {total_created} / {verb_u}: {total_updated} / "
              f"unchanged: {total_unchanged} / {verb_p}: {total_prune}")
        print(f"gh api calls made: {len(call_log)}")
        print(f"errors: {len(receipt['errors'])}")
        print("=" * 72)

        flush_receipt()
        return 0 if not receipt["errors"] else 1

    except FatalError as e:
        eprint(f"\nFATAL: {e}")
        flush_receipt()
        return 1


if __name__ == "__main__":
    sys.exit(main())
