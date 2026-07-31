from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "postmark_send_report.py"
_spec = importlib.util.spec_from_file_location("postmark_send_report_under_test", SCRIPT)
postmark = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = postmark
_spec.loader.exec_module(postmark)


def test_review_backlog_body_flags_subject():
    subject = postmark.flag_subject_for_alert("Hermes: ordinary", "REVIEW BACKLOG ALERT: 25 tasks")

    assert subject == "[ALERT] Hermes: ordinary"


def test_already_flagged_subject_not_double_prefixed():
    subject = postmark.flag_subject_for_alert("[ALERT] Hermes: ordinary", "REVIEW BACKLOG ALERT: 25 tasks")

    assert subject == "[ALERT] Hermes: ordinary"


def test_ordinary_body_keeps_subject_unchanged():
    subject = postmark.flag_subject_for_alert("Hermes: ordinary", "No blockers")

    assert subject == "Hermes: ordinary"
