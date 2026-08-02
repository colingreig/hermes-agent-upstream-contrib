"""Pins the 2026-08-02 queue-audit fix: _emit_human_merge tags a task
needs-human (+ human-merge-gate marker) when a green+mergeable PASS PR isn't
auto-merge-eligible, but nothing previously cleared that tag after a human
actually merged the PR — the task sat in the human queue forever.
Covers autonomous_merge.py::_emit_human_merge (both tags added) and
::_cleanup_human_merge_gate (tags cleared + comment posted only when the PR
is MERGED and the task still carries the human-merge-gate marker)."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
for path in (SCRIPTS, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import autonomous_merge as am  # noqa: E402


def _ops_recorder(monkeypatch, mod):
    """Patch subprocess.run inside the module and return a list of recorded
    [PY, VAL_OPS, *args] invocations (positional args only, minus PY/VAL_OPS)."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd[2:]))  # drop [PY, VAL_OPS]

        class R:
            returncode = 0
            stdout = "{}"
            stderr = ""
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return calls


def test_emit_human_merge_adds_needs_human_and_marker_tag(monkeypatch):
    calls = _ops_recorder(monkeypatch, am)

    am._emit_human_merge("acme/widget", 12, "a" * 40, "86e2xxx")

    tag_calls = [c for c in calls if c[:1] == ["add-tag"]]
    assert ["add-tag", "86e2xxx", "needs-human"] in tag_calls
    assert ["add-tag", "86e2xxx", "human-merge-gate"] in tag_calls


def test_cleanup_clears_both_tags_when_merged_and_marker_present(monkeypatch):
    """_hm_get_task is VAL_OPS `get` -> hermes_validate_ops._get_task(), which
    normalizes tags to a FLAT LIST OF STRINGS (`[tg.get("name") for tg in ...]`)
    — not `{"name": ...}` dicts. This must be the primary fixture shape: a
    dict-shaped fixture here would mask a `t.get(...)` call that raises
    AttributeError on the real string shape (swallowed by the per-entry
    `except Exception: continue`, silently no-opping cleanup on every real run)."""
    calls = _ops_recorder(monkeypatch, am)
    monkeypatch.setattr(am, "_gh_json", lambda repo, pr, fields: ({"state": "MERGED"}, None))
    monkeypatch.setattr(am, "_hm_get_task", lambda task_id: {
        "id": task_id, "tags": ["needs-human", "human-merge-gate"]})

    store = {"acme/widget#12": {"task_id": "86e2xxx"}}
    state = {"acme/widget#12": "a" * 40}

    cleaned = am._cleanup_human_merge_gate(store, state)

    assert cleaned == [{"repo": "acme/widget", "pr": 12, "task_id": "86e2xxx"}]
    rm_calls = [c for c in calls if c[:1] == ["rm-tag"]]
    assert ["rm-tag", "86e2xxx", "human-merge-gate"] in rm_calls
    assert ["rm-tag", "86e2xxx", "needs-human"] in rm_calls
    comment_calls = [c for c in calls if c[:1] == ["comment"]]
    assert len(comment_calls) == 1


def test_cleanup_tolerates_dict_shaped_tags_too(monkeypatch):
    """Defense in depth: if a future/alternate task-fetch path ever returns
    raw ClickUp {"name": ...} tag dicts instead of the normalized string list,
    cleanup must still work rather than silently no-op."""
    calls = _ops_recorder(monkeypatch, am)
    monkeypatch.setattr(am, "_gh_json", lambda repo, pr, fields: ({"state": "MERGED"}, None))
    monkeypatch.setattr(am, "_hm_get_task", lambda task_id: {
        "id": task_id, "tags": [{"name": "needs-human"}, {"name": "human-merge-gate"}]})

    store = {"acme/widget#12": {"task_id": "86e2xxx"}}
    state = {"acme/widget#12": "a" * 40}

    cleaned = am._cleanup_human_merge_gate(store, state)

    assert cleaned == [{"repo": "acme/widget", "pr": 12, "task_id": "86e2xxx"}]
    rm_calls = [c for c in calls if c[:1] == ["rm-tag"]]
    assert ["rm-tag", "86e2xxx", "human-merge-gate"] in rm_calls
    assert ["rm-tag", "86e2xxx", "needs-human"] in rm_calls


def test_cleanup_skips_when_pr_still_open(monkeypatch):
    calls = _ops_recorder(monkeypatch, am)
    monkeypatch.setattr(am, "_gh_json", lambda repo, pr, fields: ({"state": "OPEN"}, None))
    monkeypatch.setattr(am, "_hm_get_task", lambda task_id: (_ for _ in ()).throw(
        AssertionError("must not fetch the task when the PR is still open")))

    store = {"acme/widget#12": {"task_id": "86e2xxx"}}
    state = {"acme/widget#12": "a" * 40}

    cleaned = am._cleanup_human_merge_gate(store, state)

    assert cleaned == []
    assert calls == []


def test_cleanup_skips_when_marker_tag_absent(monkeypatch):
    """A task tagged needs-human for an unrelated reason (e.g. a no-measurement
    validator escalation) must never be touched by this cleanup — only tasks
    carrying the human-merge-gate marker this daemon itself set."""
    calls = _ops_recorder(monkeypatch, am)
    monkeypatch.setattr(am, "_gh_json", lambda repo, pr, fields: ({"state": "MERGED"}, None))
    monkeypatch.setattr(am, "_hm_get_task", lambda task_id: {
        "id": task_id, "tags": ["needs-human"]})

    store = {"acme/widget#12": {"task_id": "86e2xxx"}}
    state = {"acme/widget#12": "a" * 40}

    cleaned = am._cleanup_human_merge_gate(store, state)

    assert cleaned == []
    assert calls == []


def test_cleanup_skips_when_no_task_id(monkeypatch):
    calls = _ops_recorder(monkeypatch, am)
    monkeypatch.setattr(am, "_gh_json", lambda repo, pr, fields: (_ for _ in ()).throw(
        AssertionError("must not check PR state without a task id")))

    store = {"acme/widget#12": {}}
    state = {"acme/widget#12": "a" * 40}

    cleaned = am._cleanup_human_merge_gate(store, state)

    assert cleaned == []
    assert calls == []
