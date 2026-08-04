"""Shared test isolation for machine-setup/mini-scripts/tests/.

These tests load the implementation modules under test by file path
(``importlib.util.spec_from_file_location``) rather than as a real Python
package, because ``machine-setup/mini-scripts`` isn't one. Several test
files short-circuit an implementation module's own
``import autonomous_merge`` (or similar) by stashing a lightweight stub
directly into ``sys.modules`` under the REAL module's name — e.g.
test_pr_staleness_alert.py's and test_pr_pipeline_improvements.py's
``sys.modules["autonomous_merge"] = types.ModuleType("autonomous_merge")``.

Those stubs are never removed. Whichever test happens to run LAST wins that
sys.modules slot for the rest of the pytest process, so any OTHER file that
does a plain ``import autonomous_merge`` (or a function does one lazily,
e.g. adversarial_review.check_missing_ci()) can silently get handed the
stub instead of the real module. That is exactly what broke
test_pr_pipeline_wiring.py::RuntimeWiringTests::
test_visual_high_finding_forces_final_block_even_when_other_lenses_pass
when the whole directory ran together but not in isolation (ClickUp
86e2md9cw): a stub with no ``pr_state`` attribute leaked in from an
earlier-run file and crashed validate_pr.validate() after lease
acquisition.

Fix: snapshot sys.modules before every test function and restore it
exactly afterward, regardless of what the test (or the module it loads)
inserted, replaced, or deleted. This isolates test FUNCTIONS from each
other's module-cache side effects without touching the individual test
files, and it isolates in both directions — any future stub a test adds
under a real module's name is undone before the next test runs, so this
whole class of ordering bug (not just this one collision) can't recur.
"""
from __future__ import annotations

import sys
from typing import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolate_sys_modules() -> Iterator[None]:
    before = dict(sys.modules)
    try:
        yield
    finally:
        after_names = set(sys.modules)
        before_names = set(before)

        # Anything the test added that wasn't there before: gone.
        for name in after_names - before_names:
            del sys.modules[name]

        # Anything that existed before but got replaced (or removed) by the
        # test: put the original object back.
        for name in before_names:
            if sys.modules.get(name) is not before[name]:
                sys.modules[name] = before[name]
