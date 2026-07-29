#!/usr/bin/env bash
#
# Cron entry point for the spam-gate label-accrual pipeline. Runs ON mini
# (see deploy-poller.sh), not in this repo's checkout.
#
# PurelyMail appears to purge mail after roughly 5-7 days, so a single
# collect-labels.py snapshot is a narrow, decaying window of evidence. This
# wrapper accrues that evidence daily:
#
#   1. collect-labels.py pulls a fresh DRAFT labelled snapshot over a 7-day
#      IMAP window (inside PurelyMail's retention window, so it stays
#      trustworthy).
#   2. accumulate-labels.py merges that snapshot into the cumulative
#      spam-gate corpus, which is the only place evidence survives once
#      PurelyMail has purged the underlying mail.
#   3. One summary line is appended to collect.log so cron
#      successes/failures are auditable without re-running anything.
#
# Deliberately small and defensive: no flags, no cleverness. A failure in
# either Python tool is logged with its last output line, then propagated
# as this script's exit status.

set -euo pipefail

HERMES_HOME="$HOME/.hermes"
SCRIPTS_DIR="$HERMES_HOME/scripts"
STATE_DIR="$HERMES_HOME/state/spam-gate"
LOG_FILE="$STATE_DIR/collect.log"
CORPUS_FILE="$STATE_DIR/corpus.jsonl"

mkdir -p "$STATE_DIR"

log_line() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >> "$LOG_FILE"
}

last_line() {
  printf '%s' "$1" | tail -n 1
}

snapshot_tmp="$(mktemp "$STATE_DIR/.snapshot.XXXXXX")"
cleanup() {
  rm -f "$snapshot_tmp"
}
trap cleanup EXIT

if ! collect_output="$(python3 "$SCRIPTS_DIR/collect-labels.py" \
  --config "$SCRIPTS_DIR/notify-poller.config.json" \
  --secrets-file "$HERMES_HOME/secrets/purelymail-poller.env" \
  --state-dir "$HERMES_HOME/state/purelymail-poller" \
  --window-days 7 \
  --output "$snapshot_tmp" 2>&1)"; then
  log_line "FAIL collect-labels.py: $(last_line "$collect_output")"
  exit 1
fi

if ! accumulate_output="$(python3 "$SCRIPTS_DIR/accumulate-labels.py" \
  --snapshot "$snapshot_tmp" \
  --corpus "$CORPUS_FILE" 2>&1)"; then
  log_line "FAIL accumulate-labels.py: $(last_line "$accumulate_output")"
  exit 1
fi

log_line "OK $(last_line "$accumulate_output")"
