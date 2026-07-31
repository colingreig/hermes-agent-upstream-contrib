import pytest


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset mutable module-level globals that leak state between test files.

    When the full suite runs in a single process, these globals bleed across
    test files and cause order-dependent failures.
    """
    try:
        import agent.auxiliary_client as _aux

        _aux._OPENAI_CLS_CACHE = None
        _aux._LOGGED_UNKNOWN_PROVIDER_KEYS.clear()
        _aux._LOGGED_UNHANDLED_AUTHTYPE_KEYS.clear()
        _aux._LOGGED_UNSUPPORTED_EXTPROC_KEYS.clear()
        _aux._LOGGED_UNSUPPORTED_OAUTH_KEYS.clear()
        _aux._VISION_EXHAUSTION_ALERT_KEYS.clear()
        _aux._WARNED_KEEPALIVE_IMPORT_SKEW = False
        _aux._stale_base_url_warned = False
        _aux.auxiliary_is_nous = False
        _aux._RUNTIME_MAIN_PROVIDER = ""
        _aux._RUNTIME_MAIN_MODEL = ""
        _aux._RUNTIME_MAIN_BASE_URL = ""
        _aux._RUNTIME_MAIN_API_KEY = ""
        _aux._RUNTIME_MAIN_API_MODE = ""
        _aux._RUNTIME_MAIN_AUTH_MODE = ""
        _aux._RUNTIME_MAIN_COMPAT_SNAPSHOT = ("", "", "", "", "", "")
        _aux._reset_aux_unhealthy_cache()
        _aux._client_cache.clear()
    except ImportError:
        pass

    try:
        import agent.model_metadata as _mm

        _mm._model_metadata_cache.clear()
        _mm._model_metadata_cache_time = 0
        _mm._novita_metadata_cache.clear()
        _mm._novita_metadata_cache_time = 0
        _mm._endpoint_model_metadata_cache.clear()
        _mm._endpoint_model_metadata_cache_time.clear()
        _mm._endpoint_probe_path_cache.clear()
        _mm._LOCAL_CTX_PROBE_CACHE.clear()
        _mm._codex_oauth_context_cache.clear()
        _mm._codex_oauth_context_cache_time = 0.0
        _mm._TOOLS_TOKENS_CACHE.clear()
    except ImportError:
        pass

    try:
        import agent.credits_tracker as _ct

        _ct._version_warning_emitted = False
    except ImportError:
        pass

    try:
        import tools.file_tools as _ft

        _ft._hermes_config_resolved = None
        _ft._hermes_config_resolved_loaded = False
    except ImportError:
        pass

    try:
        from agent.image_gen_registry import _reset_for_tests as _reset_image_gen

        _reset_image_gen()
    except ImportError:
        pass

    try:
        from agent.video_gen_registry import _reset_for_tests as _reset_video_gen

        _reset_video_gen()
    except ImportError:
        pass

    try:
        from agent.tts_registry import _reset_for_tests as _reset_tts

        _reset_tts()
    except ImportError:
        pass

    try:
        from agent.transcription_registry import _reset_for_tests as _reset_transcription

        _reset_transcription()
    except ImportError:
        pass

    try:
        from agent.browser_registry import _reset_for_tests as _reset_browser

        _reset_browser()
    except ImportError:
        pass

    try:
        from agent.web_search_registry import _reset_for_tests as _reset_web_search

        _reset_web_search()
    except ImportError:
        pass

    try:
        from agent import portal_tags as _pt

        _pt._conversation_id.set(None)
    except ImportError:
        pass

    yield

    try:
        from agent import portal_tags as _pt

        _pt._conversation_id.set(None)
    except ImportError:
        pass


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

