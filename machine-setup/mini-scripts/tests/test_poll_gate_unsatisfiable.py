# ~/.hermes/scripts/tests/test_poll_gate_unsatisfiable.py
#
# Tests for the structurally-unsatisfiable verify gate detector (2026-06-19,
# ClickUp 86e1ynuw1). Loads clickup_poll_gate.py via importlib so the test
# runs against the actual production module without invoking main().
#
# Acceptance criteria from the task:
#   - _classify() distinguishes un-retryable verify failures from healthy
#     continuations via the agent-ready + park-blocked-external tag combo.
#   - A task matching the pattern is parked after FIRST detection: agent-ready
#     removed, status moved off in progress, NOT re-woken on next cycle.
#   - Healthy multi-iteration tasks are NOT false-parked.
#
# Test scope is _classify() behavior only — the full cron wake loop is
# exercised separately by the dry-run smoke test against live ClickUp state.
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "poll_gate", os.path.expanduser("~/.hermes/scripts/clickup_poll_gate.py"))
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


def _task(status, tags, status_type="open"):
    """Build a minimal task dict shaped like the ClickUp API response."""
    return {
        "id": "t1", "name": "x", "url": "http://x",
        "status": {"status": status, "type": status_type},
        "tags": [{"name": n} for n in tags],
        "list": {"name": "L", "id": "1"},
    }


# ---- Baseline regression: existing _classify() return values are preserved.

def test_unclaimed_when_to_do_and_agent_ready():
    assert pg._classify(_task("to do", ["agent-ready"])) == "unclaimed"


def test_continuation_when_in_progress_and_agent_ready():
    assert pg._classify(_task("in progress", ["agent-ready"])) == "continuation"


def test_none_when_no_agent_ready():
    assert pg._classify(_task("in progress", [])) is None
    assert pg._classify(_task("to do", ["other-tag"])) is None


def test_none_when_terminal_status_with_agent_ready():
    # Defensive: a task in a closed/done state must never be re-woken even
    # with agent-ready (which can linger as a stale tag).
    assert pg._classify(_task("complete", ["agent-ready"],
                              status_type="closed")) is None


def test_none_when_in_review_with_agent_ready():
    # in review is human-move territory; agent-ready tag shouldn't override.
    assert pg._classify(_task("in review", ["agent-ready"])) is None


# ---- NEW: structurally-unsatisfiable detection (the bug fix).

def test_parked_when_agent_ready_and_park_blocked_external_and_in_progress():
    """The core fix: continuation candidate with park-blocked-external tag
    must be classified as 'parked' (not 'continuation'), so the gate drops
    agent-ready and does NOT wake the executor."""
    result = pg._classify(
        _task("in progress", ["agent-ready", "park-blocked-external"]))
    assert result == "parked", f"expected 'parked', got {result!r}"


def test_parked_takes_precedence_over_continuation():
    """Tag order doesn't matter — the park-blocked-external tag must dominate
    the continuation detection regardless of position in the tag list."""
    a = pg._classify(_task("in progress", ["agent-ready", "park-blocked-external"]))
    b = pg._classify(_task("in progress", ["park-blocked-external", "agent-ready"]))
    assert a == "parked" and b == "parked"


def test_park_tag_alone_without_agent_ready_is_ignored():
    """The park tag is a SIGNAL not a TRIGGER. Without agent-ready the task
    isn't in the wake set anyway, so classification must be None (defensive
    in case the gate ever sees such a task in the direct-GET recovery path)."""
    assert pg._classify(_task("in progress", ["park-blocked-external"])) is None


def test_park_tag_on_unclaimed_task_is_unclaimed():
    """A to-do task with the park tag and agent-ready should still be visible
    as unclaimed so the next cron picks it up and the executor can deal with
    it (e.g. operator re-armed it). The park tag only suppresses the
    continuation wake — it doesn't make the task invisible overall."""
    # Per current design the park tag is stamped by the executor AFTER it
    # decides to park; a to-do + park + agent-ready combo shouldn't normally
    # exist. But if it does (operator did a manual re-tag), the safest answer
    # is unclaimed: let the executor see the live comments and decide.
    assert pg._classify(
        _task("to do", ["agent-ready", "park-blocked-external"])) == "unclaimed"


# ---- Defensive: the new tag must not collide with any other tag in the
# ---- existing flow (we don't want this regressing on tag-typo cases).

def test_unknown_tag_with_agent_ready_still_continues():
    """Sanity: arbitrary other tags don't trigger the parked path. Only
    the specific 'park-blocked-external' tag does."""
    assert pg._classify(_task("in progress",
                              ["agent-ready", "needs-validation"])) == "continuation"
    # NOTE: agent-review + agent-ready is a decision-park half-state and must
    # classify None (the 86e1yxn5e un-park-loop guard) — not 'continuation'.
    assert pg._classify(_task("in progress",
                              ["agent-ready", "agent-review"])) is None
    assert pg._classify(_task("in progress",
                              ["agent-ready", "park-some-other-thing"])) == "continuation"


# ---- NEW: agent-avoid operator hard-fence (2026-06-19, Colin).

def test_avoid_tag_excludes_unclaimed():
    """agent-avoid on an otherwise-claimable to-do task → None (invisible)."""
    assert pg._classify(_task("to do", ["agent-ready", "agent-avoid"])) is None


def test_avoid_tag_excludes_continuation():
    """agent-avoid dominates the continuation path for an in-progress task."""
    assert pg._classify(_task("in progress", ["agent-ready", "agent-avoid"])) is None


def test_avoid_tag_dominates_park_blocked_external():
    """agent-avoid wins even over the self-park signal — no classification,
    so main() takes NO side effect (no tag mutation) on a fenced task."""
    assert pg._classify(
        _task("in progress",
              ["agent-ready", "park-blocked-external", "agent-avoid"])) is None


def test_avoid_tag_wins_regardless_of_order():
    """Tag position is irrelevant — the avoid check is first and absolute."""
    assert pg._classify(_task("to do", ["agent-avoid", "agent-ready"])) is None
    assert pg._classify(_task("in progress", ["agent-avoid", "agent-ready"])) is None


def test_avoid_tag_alone_is_none():
    """Without agent-ready the task wasn't wakeable anyway; avoid keeps it None."""
    assert pg._classify(_task("to do", ["agent-avoid"])) is None
