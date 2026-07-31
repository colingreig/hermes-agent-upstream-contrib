import pytest


@pytest.fixture(autouse=True)
def _reset_session_context():
    """Reset gateway session context between tests to prevent ContextVar bleed.

    Tests such as test_skill_commands.py call ``set_session_vars(...)`` /
    ``clear_session_vars()`` directly. ``clear_session_vars`` intentionally
    sets the ContextVars to ``""`` (an "explicitly cleared" state) rather than
    back to the ``_UNSET`` sentinel, so any later test in the same worker that
    relies on the ``os.environ`` fallback in ``get_session_env`` silently gets
    ``""`` instead. ``reset_session_vars`` restores the true ``_UNSET``
    "never bound in this context" state, matching what a fresh test should see.
    """
    yield
    try:
        from gateway.session_context import reset_session_vars

        reset_session_vars()
    except ImportError:
        pass

