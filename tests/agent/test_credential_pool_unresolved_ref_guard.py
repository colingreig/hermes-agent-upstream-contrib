"""Defense-in-depth: the credential pool must never register a candidate
credential whose value is still an un-interpolated ``${VAR}``/``$VAR``
reference.

This is layer 2 of the fix for the fleet-wide Gemini outage. Layer 1
(``hermes_cli.config.load_env``) now interpolates ``${VAR}`` references and
skips keys it can't resolve -- but other seeding paths (e.g.
``custom_providers`` in config.yaml, expanded by the more permissive
``_expand_env_vars`` which intentionally keeps unresolved refs verbatim so
users can spot them in the YAML) could still hand ``_upsert_entry`` a raw
``${GEMINI_API_KEY}``-shaped literal. This guard is the single choke point
every seeding path (``_seed_from_env``, ``_seed_from_singletons``,
``_seed_custom_pool``) passes through, so it catches the poison literal
regardless of which upstream path produced it.
"""

from agent.credential_pool import _looks_like_unresolved_env_ref, _upsert_entry


class TestLooksLikeUnresolvedEnvRef:
    def test_braced_reference_is_flagged(self):
        assert _looks_like_unresolved_env_ref("${GEMINI_API_KEY}") is True

    def test_bare_var_reference_is_flagged(self):
        assert _looks_like_unresolved_env_ref("$GEMINI_API_KEY") is True

    def test_real_secret_is_not_flagged(self):
        assert _looks_like_unresolved_env_ref("AIzaSy-real-secret-value") is False

    def test_real_secret_containing_a_dollar_sign_is_not_flagged(self):
        # A real secret that merely contains a $ (not shaped like a whole
        # reference) must not be treated as unresolved.
        assert _looks_like_unresolved_env_ref("sk-abc$123") is False

    def test_non_string_is_not_flagged(self):
        assert _looks_like_unresolved_env_ref(None) is False
        assert _looks_like_unresolved_env_ref(123) is False


class TestUpsertEntryRejectsUnresolvedRef:
    def test_skips_candidate_whose_token_is_literally_the_reference(self):
        """The exact incident shape: access_token == '${GEMINI_API_KEY}'."""
        entries = []
        changed = _upsert_entry(
            entries,
            "google",
            "env:GOOGLE_API_KEY",
            {
                "source": "env:GOOGLE_API_KEY",
                "auth_type": "api_key",
                "access_token": "${GEMINI_API_KEY}",
                "base_url": "https://generativelanguage.googleapis.com",
                "label": "GOOGLE_API_KEY",
            },
        )

        assert changed is False
        assert entries == [], (
            "An un-interpolated ${VAR} literal must never be registered as a "
            f"usable credential; pool contains: {entries}"
        )

    def test_skips_candidate_whose_token_is_a_bare_var_reference(self):
        entries = []
        changed = _upsert_entry(
            entries,
            "google",
            "env:GOOGLE_API_KEY",
            {
                "source": "env:GOOGLE_API_KEY",
                "auth_type": "api_key",
                "access_token": "$GEMINI_API_KEY",
                "base_url": "https://generativelanguage.googleapis.com",
                "label": "GOOGLE_API_KEY",
            },
        )

        assert changed is False
        assert entries == []

    def test_real_credential_still_registers_normally(self):
        """No-regression check: a legitimate token is still accepted."""
        entries = []
        changed = _upsert_entry(
            entries,
            "google",
            "env:GOOGLE_API_KEY",
            {
                "source": "env:GOOGLE_API_KEY",
                "auth_type": "api_key",
                "access_token": "AIzaSy-real-secret-value",
                "base_url": "https://generativelanguage.googleapis.com",
                "label": "GOOGLE_API_KEY",
            },
        )

        assert changed is True
        assert len(entries) == 1
        assert entries[0].access_token == "AIzaSy-real-secret-value"
