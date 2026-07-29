# ~/.hermes/scripts/tests/test_poll_gate_localization.py
#
# Tests for the localization / i18n / translation hard exclusion
# (2026-06-20, ClickUp 86e1z1fy0). Loads clickup_poll_gate.py via importlib
# so the test runs against the actual production module without invoking
# main().
#
# Acceptance criteria from the task body:
#   - A localization task tagged agent-ready is NOT claimed (None from
#     _classify); the skip is logged with a reason.
#   - Non-localization agent-ready tasks are still classified normally
#     (no regression).
#   - Three defense-in-depth signals: deny-list, title-keyword regex, and
#     positive `no-agent` tag convention.
#   - The general `agent-avoid` tag is NOT a localization signal (it has
#     its own first check; conflating them would log misleading reasons).
#
# Test scope is _classify() + _is_localization_task() behavior only — the
# full cron wake loop is exercised separately by the dry-run smoke test
# against live ClickUp state.
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location(
    "poll_gate", os.path.expanduser("~/.hermes/scripts/clickup_poll_gate.py"))
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)


def _task(task_id="t1", name="x", status="to do", tags=None, list_id="L",
          status_type="open"):
    """Build a minimal task dict shaped like the ClickUp API response."""
    return {
        "id": task_id, "name": name, "url": "http://x",
        "status": {"status": status, "type": status_type},
        "tags": [{"name": n} for n in (tags or [])],
        "list": {"name": "L", "id": list_id},
    }


def _is_loc(task):
    """Helper: capture the localization verdict (skip stderr noise)."""
    saved = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        return pg._is_localization_task(task)
    finally:
        sys.stderr.close()
        sys.stderr = saved


# ---- Title-keyword: word-boundary match on localization words.

def test_keyword_locale_matches():
    is_loc, reason = _is_loc(_task(name="[OEC] Locale build-out"))
    assert is_loc and "title-keyword" in reason


def test_keyword_locales_matches():
    is_loc, _ = _is_loc(_task(name="Build locales dictionary"))
    assert is_loc


def test_keyword_translation_matches():
    is_loc, _ = _is_loc(_task(name="WP translation queue"))
    assert is_loc


def test_keyword_translations_matches():
    is_loc, _ = _is_loc(_task(name="Reconcile translations after deploy"))
    assert is_loc


def test_keyword_i18n_matches():
    is_loc, _ = _is_loc(_task(name="i18n audit"))
    assert is_loc


def test_keyword_hreflang_matches():
    is_loc, _ = _is_loc(_task(name="hreflang linkage check"))
    assert is_loc


def test_keyword_oec_prefix_matches_with_locale_word():
    """[OEC] tag + any localization keyword — broader match for OEC list."""
    is_loc, _ = _is_loc(_task(name="[OEC] fix the translation reconciliation"))
    assert is_loc


def test_keyword_oec_prefix_alone_does_not_match():
    """[OEC] alone is not a localization signal — must also have a localization word."""
    is_loc, _ = _is_loc(_task(name="[OEC] rebuild the contact form"))
    assert not is_loc


def test_keyword_case_insensitive():
    is_loc, _ = _is_loc(_task(name="LOCALE BUILD-OUT"))
    assert is_loc


def test_keyword_word_boundary_blocks_false_match():
    """`locales` and `locale` are word-boundary anchored — `geolocation` is not."""
    is_loc, _ = _is_loc(_task(name="Geolocation accuracy"))
    assert not is_loc


def test_keyword_no_match_on_normal_task():
    is_loc, _ = _is_loc(_task(name="[jdmbuysell] Aggregator ingest"))
    assert not is_loc


# ---- Deny-list: explicit operator-curated task ids.

def test_deny_list_matches_known_offender():
    """86e1yxn5e is in the default deny-list (verified 2026-06-20)."""
    is_loc, reason = _is_loc(_task(task_id="86e1yxn5e", name="any name"))
    assert is_loc and reason == "deny-list"


def test_deny_list_matches_other_known_offenders():
    for tid in ("86e1yq89q", "86e1z13vv"):
        is_loc, _ = _is_loc(_task(task_id=tid, name="any"))
        assert is_loc, f"{tid} should be in default deny-list"


def test_deny_list_does_not_match_normal_task():
    is_loc, _ = _is_loc(_task(task_id="86e1uxqjj", name="Build Plan"))
    assert not is_loc


def test_deny_list_loads_from_file(monkeypatch=None, tmp_path=None):
    """Operator can add ids to ~/.hermes/scripts/localization_deny_ids.json
    without code change. Verify the loader picks up file content over defaults.
    """
    import json, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"ids": ["86etest99"]}, f)
        path = f.name
    saved = pg.LOCALIZATION_DENY_IDS_PATH
    pg.LOCALIZATION_DENY_IDS_PATH = path
    try:
        is_loc, reason = pg._is_localization_task(_task(task_id="86etest99"))
        assert is_loc and reason == "deny-list"
        # An id NOT in the file and NOT in defaults → not localized
        is_loc2, _ = pg._is_localization_task(_task(task_id="86eother00"))
        assert not is_loc2
    finally:
        pg.LOCALIZATION_DENY_IDS_PATH = saved
        os.unlink(path)


def test_deny_list_file_missing_falls_back_to_defaults():
    """No file present → defaults from code apply."""
    saved = pg.LOCALIZATION_DENY_IDS_PATH
    pg.LOCALIZATION_DENY_IDS_PATH = "/tmp/does-not-exist-987654321.json"
    try:
        is_loc, reason = pg._is_localization_task(_task(task_id="86e1yxn5e"))
        assert is_loc and reason == "deny-list"
    finally:
        pg.LOCALIZATION_DENY_IDS_PATH = saved


# ---- `no-agent` tag convention.

def test_no_agent_tag_matches():
    is_loc, reason = _is_loc(_task(name="any name", tags=["no-agent"]))
    assert is_loc and reason == "tag:no-agent"


def test_no_agent_tag_dominates():
    """The positive tag signal fires even when title is innocuous."""
    is_loc, reason = _is_loc(_task(name="random task", tags=["agent-ready", "no-agent"]))
    assert is_loc and reason == "tag:no-agent"


def test_agent_avoid_tag_does_not_count_as_localization():
    """The general agent-avoid tag is NOT a localization signal — it has its
    own first-check in _classify(). Including it here would log misleading
    reasons for non-localization fences."""
    is_loc, reason = _is_loc(_task(name="general hands-off task",
                                   tags=["agent-avoid"]))
    assert not is_loc, f"got reason={reason!r}, expected (False, None)"


# ---- _classify(): end-to-end classification with localization exclusion.

def test_classify_none_for_localization_keyword():
    """A localization task tagged agent-ready returns None — Hermes never sees it."""
    result = pg._classify(_task(name="[OEC] Locale build-out",
                                tags=["agent-ready"]))
    assert result is None


def test_classify_none_for_localization_in_deny_list():
    """Deny-list members are invisible to _classify even with benign name."""
    result = pg._classify(_task(task_id="86e1yxn5e",
                                name="benign-looking name",
                                tags=["agent-ready"]))
    assert result is None


def test_classify_none_for_no_agent_tag():
    result = pg._classify(_task(name="benign task",
                                tags=["agent-ready", "no-agent"]))
    assert result is None


def test_classify_unclaimed_for_non_localization():
    """Regression guard: a non-localization task with agent-ready is still
    classified as unclaimed — the new exclusion does NOT regress the queue."""
    result = pg._classify(_task(name="[jdmbuysell] aggregator ingest",
                                tags=["agent-ready"]))
    assert result == "unclaimed"


def test_classify_continuation_for_non_localization():
    """A non-localization in-progress + agent-ready task is still resumable."""
    result = pg._classify(_task(name="[elevatoruptime] hub maintenance",
                                status="in progress",
                                tags=["agent-ready"]))
    assert result == "continuation"


def test_classify_localization_check_runs_before_agent_ready():
    """The localization check is FIRST — even tasks WITHOUT agent-ready are
    invisible (no risk of accidental claim on a future re-tag)."""
    result = pg._classify(_task(name="[OEC] Locale build-out", tags=[]))
    assert result is None


def test_classify_localization_skips_with_reason_logged():
    """Capture stderr — the skip must be observable, not silent."""
    import io
    buf = io.StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        pg._classify(_task(name="[OEC] Locale build-out",
                            tags=["agent-ready"]))
    finally:
        sys.stderr = saved
    out = buf.getvalue()
    assert "localization skip" in out
    assert "reason=title-keyword" in out


def test_classify_deny_list_logs_deny_list_reason():
    import io
    buf = io.StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        pg._classify(_task(task_id="86e1yxn5e", name="benign",
                            tags=["agent-ready"]))
    finally:
        sys.stderr = saved
    out = buf.getvalue()
    assert "localization skip" in out
    assert "reason=deny-list" in out


def test_classify_no_agent_tag_logs_tag_reason():
    import io
    buf = io.StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        pg._classify(_task(name="benign", tags=["agent-ready", "no-agent"]))
    finally:
        sys.stderr = saved
    out = buf.getvalue()
    assert "localization skip" in out
    assert "reason=tag:no-agent" in out


# ---- Edge cases.

def test_empty_name_does_not_crash():
    is_loc, _ = _is_loc(_task(name=""))
    assert not is_loc


def test_no_name_field_does_not_crash():
    task = _task()
    del task["name"]
    is_loc, _ = _is_loc(task)
    assert not is_loc


def test_localization_predicate_precedence_deny_wins():
    """Deny-list check runs first — even a task with NO title keyword matches."""
    is_loc, reason = _is_loc(_task(task_id="86e1yxn5e",
                                   name="totally unrelated task name"))
    assert is_loc and reason == "deny-list"


def test_localization_predicate_precedence_tag_after_deny():
    """When neither deny-list nor title matches, the tag check fires."""
    is_loc, reason = _is_loc(_task(task_id="86enormal",
                                   name="totally unrelated task",
                                   tags=["no-agent"]))
    assert is_loc and reason == "tag:no-agent"


def test_localization_predicate_precedence_keyword_after_tag():
    """Title-keyword check is LAST (cheapest + most-broad false-positive risk)."""
    is_loc, reason = _is_loc(_task(task_id="86enormal",
                                   name="[OEC] Locale build-out",
                                   tags=[]))
    assert is_loc and "title-keyword" in reason


# ---- Meta-task exemption (NEW 2026-06-20): tasks that TALK ABOUT the
# ---- localization exclusion (e.g. "[Hermes] Task-selector must exclude ...")
# ---- are NOT themselves localization tasks and must remain claimable.

def test_meta_hermes_tag_exempts():
    """[Hermes] prefix means meta-task — should NOT be flagged."""
    is_loc, reason = _is_loc(_task(name="[Hermes] Task-selector must exclude localization/translation"))
    assert not is_loc, f"got reason={reason!r}"


def test_meta_exclude_keyword_exempts():
    """Title with 'exclude' on a localization word is meta, not localization work."""
    is_loc, _ = _is_loc(_task(name="Add option to exclude translations from rebuild"))
    assert not is_loc


def test_meta_minus_i18n_exempts():
    """'... minus i18n' is a meta-task that explicitly excludes i18n."""
    is_loc, _ = _is_loc(_task(name="Phase 0 · Foundation (clone OEC architecture, minus i18n)"))
    assert not is_loc


def test_meta_never_claim_exempts():
    """'never claim' is meta-discussion about the exclusion."""
    is_loc, _ = _is_loc(_task(name="Localization tasks must never claim by Hermes"))
    assert not is_loc


def test_meta_selector_exempts():
    """'selector' (the task-selector) is meta."""
    is_loc, _ = _is_loc(_task(name="[Hermes] selector must skip i18n tasks"))
    assert not is_loc


def test_real_localization_not_exempted_by_meta_check():
    """Tasks that mention a localization keyword AND do real localization
    work must STILL be flagged (the meta-pattern doesn't blanket-exempt)."""
    # 'sitemap + hreflang' is a real localization task, not meta.
    is_loc, _ = _is_loc(_task(name="sitemap.xml emission + canonicals [hreflang]"))
    assert is_loc
    # 'all locales' is real work.
    is_loc, _ = _is_loc(_task(name="Rebuild /company page for all locales"))
    assert is_loc


def test_meta_exemption_runs_after_title_keyword():
    """The deny-list and tag checks fire BEFORE the title-keyword+meta path."""
    # A deny-listed id is excluded regardless of title.
    is_loc, reason = _is_loc(_task(task_id="86e1yxn5e",
                                   name="[Hermes] some meta about excluding..."))
    assert is_loc and reason == "deny-list"
    # A no-agent tag is excluded regardless of title.
    is_loc, reason = _is_loc(_task(name="[Hermes] meta task",
                                   tags=["no-agent"]))
    assert is_loc and reason == "tag:no-agent"
