"""Coverage for the source-gated deployed-preview visual validation pilot."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import validator_visual_preview as vvp  # noqa: E402


HEAD = "a" * 40
PREVIEW = "https://pr-412.preview.jdmbuysell.com/article"


class ConfigTests(unittest.TestCase):
    def test_source_default_and_explicit_false_are_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(vvp.visual_validation_enabled(root / "missing.yaml"))

            disabled = root / "disabled.yaml"
            disabled.write_text(
                "content_pipeline:\n"
                "  visual_validation:\n"
                "    enabled: false\n"
            )
            self.assertFalse(vvp.visual_validation_enabled(disabled))

    def test_malformed_or_non_boolean_config_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            malformed = root / "malformed.yaml"
            malformed.write_text("content_pipeline: [\n")
            self.assertFalse(vvp.visual_validation_enabled(malformed))

            string_value = root / "string.yaml"
            string_value.write_text(
                "content_pipeline:\n"
                "  visual_validation:\n"
                '    enabled: "true"\n'
            )
            self.assertFalse(vvp.visual_validation_enabled(string_value))

    def test_exact_nested_boolean_enables_pilot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "content_pipeline:\n"
                "  visual_validation:\n"
                "    enabled: true\n"
            )
            self.assertTrue(vvp.visual_validation_enabled(config))


class PreviewSelectionTests(unittest.TestCase):
    @staticmethod
    def _check_run(
        *,
        app_id: int = vvp.PREVIEW_CHECK_APP_ID,
        head: str = HEAD,
        conclusion: str = "success",
        marker_head: str = HEAD,
        url: str = PREVIEW,
    ) -> dict:
        marker = vvp.PREVIEW_MARKER + json.dumps(
            {"head_sha": marker_head, "url": url},
            separators=(",", ":"),
        )
        return {
            "id": 321,
            "name": vvp.PREVIEW_CHECK_NAME,
            "head_sha": head,
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": "2026-07-28T10:00:00Z",
            "app": {"id": app_id},
            "output": {"title": "Preview ready", "summary": marker, "text": ""},
        }

    def test_approved_app_head_bound_check_marker_is_selected(self) -> None:
        response = {"check_runs": [self._check_run()]}
        with mock.patch.object(vvp, "_gh_api_json", return_value=response) as api:
            self.assertEqual(vvp.resolve_preview_url(vvp.PILOT_REPO, HEAD), PREVIEW)
        api.assert_called_once_with(
            f"repos/{vvp.PILOT_REPO}/commits/{HEAD}/check-runs?per_page=100"
        )

    def test_unapproved_app_wrong_head_and_unsuccessful_checks_are_rejected(self) -> None:
        response = {
            "check_runs": [
                self._check_run(app_id=1),
                self._check_run(head="b" * 40),
                self._check_run(marker_head="b" * 40),
                self._check_run(conclusion="failure"),
            ]
        }
        with mock.patch.object(vvp, "_gh_api_json", return_value=response):
            with self.assertRaisesRegex(vvp.VisualPreviewError, "no approved head-bound"):
                vvp.resolve_preview_url(vvp.PILOT_REPO, HEAD)

    def test_control_plane_details_and_non_https_urls_are_rejected(self) -> None:
        rejected = (
            "http://preview.example.test/article",
            "https://dashboard.example.test/build/1",
            "https://preview.example.test/details/1",
            "https://github.com/org/repo/deployments/1",
        )
        for value in rejected:
            self.assertEqual(vvp._eligible_preview_url(value), "", value)
        self.assertEqual(vvp._eligible_preview_url(PREVIEW), PREVIEW)
        self.assertEqual(vvp._eligible_preview_url(None), "")

    def test_real_current_state_without_preview_producer_fails_closed(self) -> None:
        with mock.patch.object(vvp, "_gh_api_json", return_value={"check_runs": []}):
            with self.assertRaisesRegex(
                vvp.VisualPreviewError,
                "provision the separate 'Hermes PR Preview' producer",
            ):
                vvp.resolve_preview_url(vvp.PILOT_REPO, HEAD)


class VisualPilotRunTests(unittest.TestCase):
    def _run_with_tools(
        self,
        *,
        navigate: object = None,
        vision: object = None,
        resolve: object = PREVIEW,
    ) -> tuple[dict, mock.Mock]:
        navigate_result = (
            json.dumps({"success": True, "url": PREVIEW})
            if navigate is None
            else navigate
        )
        vision_result = (
            json.dumps(
                {
                    "success": True,
                    "analysis": json.dumps(
                        {"verdict": "PASS", "reason": "page is complete and readable"}
                    ),
                }
            )
            if vision is None
            else vision
        )
        cleanup = mock.Mock()
        resolve_effect = (
            {"return_value": resolve}
            if not isinstance(resolve, BaseException)
            else {"side_effect": resolve}
        )
        with (
            mock.patch.object(vvp, "visual_validation_enabled", return_value=True),
            mock.patch.object(vvp, "resolve_preview_url", **resolve_effect),
            mock.patch.object(vvp, "browser_navigate", return_value=navigate_result),
            mock.patch.object(vvp, "browser_vision", return_value=vision_result),
            mock.patch.object(vvp, "cleanup_browser", cleanup),
        ):
            result = vvp.run(repo=vvp.PILOT_REPO, pr=412, head=HEAD)
        return result, cleanup

    def test_wrong_repository_is_noop_before_config_or_browser(self) -> None:
        with (
            mock.patch.object(
                vvp,
                "visual_validation_enabled",
                side_effect=AssertionError("config should not be read"),
            ),
            mock.patch.object(
                vvp,
                "cleanup_browser",
                side_effect=AssertionError("browser should not be touched"),
            ),
        ):
            result = vvp.run(repo="other/project", pr=1, head=HEAD)
        self.assertEqual(result["verdict"], "SKIP")
        self.assertEqual(result["status"], "not-pilot")
        self.assertEqual(result["findings"], [])

    def test_disabled_pilot_is_noop(self) -> None:
        with (
            mock.patch.object(vvp, "visual_validation_enabled", return_value=False),
            mock.patch.object(vvp, "resolve_preview_url") as resolve,
            mock.patch.object(vvp, "cleanup_browser") as cleanup,
        ):
            result = vvp.run(repo=vvp.PILOT_REPO, pr=412, head=HEAD)
        self.assertEqual(result["verdict"], "SKIP")
        self.assertEqual(result["status"], "disabled")
        resolve.assert_not_called()
        cleanup.assert_not_called()

    def test_missing_preview_is_high_and_cleans_up(self) -> None:
        result, cleanup = self._run_with_tools(
            resolve=vvp.VisualPreviewError("nothing deployed")
        )
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["failure_class"], "missing-preview")
        self.assertEqual(result["findings"][0]["severity"], "high")
        cleanup.assert_called_once()

    def test_navigation_failure_is_high_and_cleans_up(self) -> None:
        result, cleanup = self._run_with_tools(
            navigate=json.dumps({"success": False, "error": "timeout"})
        )
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["failure_class"], "navigation-failure")
        self.assertEqual(result["findings"][0]["severity"], "high")
        cleanup.assert_called_once()

    def test_vision_failure_is_high_and_cleans_up(self) -> None:
        result, cleanup = self._run_with_tools(
            vision=json.dumps({"success": False, "error": "vision unavailable"})
        )
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["failure_class"], "vision-failure")
        self.assertEqual(result["findings"][0]["severity"], "high")
        cleanup.assert_called_once()

    def test_unparseable_vision_result_is_high_and_cleans_up(self) -> None:
        result, cleanup = self._run_with_tools(
            vision=json.dumps({"success": True, "analysis": "VERDICT: PASS"})
        )
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["failure_class"], "parse-failure")
        self.assertEqual(result["findings"][0]["severity"], "high")
        cleanup.assert_called_once()

    def test_native_envelope_is_routed_through_auxiliary_vision(self) -> None:
        native = {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": "native fast-path note"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,c2NyZWVuc2hvdA=="},
                },
            ],
            "text_summary": "Screenshot attached natively",
            "meta": {"screenshot_path": "/tmp/preview.png"},
        }
        cleanup = mock.Mock()
        auxiliary = mock.Mock(
            return_value=json.dumps(
                {"verdict": "PASS", "reason": "preview renders correctly"}
            )
        )
        with (
            mock.patch.object(vvp, "visual_validation_enabled", return_value=True),
            mock.patch.object(vvp, "resolve_preview_url", return_value=PREVIEW),
            mock.patch.object(
                vvp,
                "browser_navigate",
                return_value=json.dumps({"success": True}),
            ),
            mock.patch.object(vvp, "browser_vision", return_value=native),
            mock.patch.object(vvp, "call_auxiliary_vision", auxiliary),
            mock.patch.object(vvp, "cleanup_browser", cleanup),
        ):
            result = vvp.run(repo=vvp.PILOT_REPO, pr=412, head=HEAD)

        self.assertEqual(result["verdict"], "PASS")
        content = auxiliary.call_args.args[0]
        self.assertEqual(content[0], {"type": "text", "text": vvp._VISION_PROMPT})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertNotIn("native fast-path note", json.dumps(content))
        cleanup.assert_called_once()

    def test_native_envelope_auxiliary_failure_is_high(self) -> None:
        native = {
            "_multimodal": True,
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,c2NyZWVuc2hvdA=="},
                }
            ],
        }
        cleanup = mock.Mock()
        with (
            mock.patch.object(vvp, "visual_validation_enabled", return_value=True),
            mock.patch.object(vvp, "resolve_preview_url", return_value=PREVIEW),
            mock.patch.object(
                vvp,
                "browser_navigate",
                return_value=json.dumps({"success": True}),
            ),
            mock.patch.object(vvp, "browser_vision", return_value=native),
            mock.patch.object(
                vvp,
                "call_auxiliary_vision",
                side_effect=RuntimeError("auxiliary.vision unavailable"),
            ),
            mock.patch.object(vvp, "cleanup_browser", cleanup),
        ):
            result = vvp.run(repo=vvp.PILOT_REPO, pr=412, head=HEAD)

        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["failure_class"], "vision-failure")
        self.assertEqual(result["findings"][0]["severity"], "high")
        cleanup.assert_called_once()

    def test_auxiliary_wrapper_uses_supported_vision_task(self) -> None:
        import sys

        response = object()
        content = [
            {"type": "text", "text": vvp._VISION_PROMPT},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,c2NyZWVuc2hvdA=="},
            },
        ]
        auxiliary_client = mock.MagicMock()
        auxiliary_client.call_llm.return_value = response
        auxiliary_client.extract_content_or_reasoning.return_value = (
            '{"verdict":"PASS","reason":"rendered"}'
        )
        with mock.patch.dict(sys.modules, {"agent.auxiliary_client": auxiliary_client}):
            result = vvp.call_auxiliary_vision(content)

        self.assertEqual(result, '{"verdict":"PASS","reason":"rendered"}')
        self.assertEqual(auxiliary_client.call_llm.call_args.kwargs["task"], "vision")
        self.assertEqual(
            auxiliary_client.call_llm.call_args.kwargs["messages"],
            [{"role": "user", "content": content}],
        )
        auxiliary_client.extract_content_or_reasoning.assert_called_once_with(response)

    def test_pass_is_normalized_to_info_and_cleans_up(self) -> None:
        fenced = json.dumps(
            {
                "success": True,
                "analysis": (
                    "```json\n"
                    '{"verdict":"PASS","reason":"layout and assets render correctly"}'
                    "\n```"
                ),
            }
        )
        result, cleanup = self._run_with_tools(vision=fenced)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["preview_url"], PREVIEW)
        self.assertEqual(result["findings"][0]["severity"], "info")
        cleanup.assert_called_once()

    def test_enabled_pilot_drives_navigate_then_vision_then_cleanup(self) -> None:
        calls: list[tuple[str, str]] = []

        def navigate(url: str, *, task_id: str) -> str:
            calls.append(("navigate", f"{url}|{task_id}"))
            return json.dumps({"success": True})

        def vision(question: str, *, task_id: str) -> str:
            self.assertIn("Return ONLY one JSON object", question)
            calls.append(("vision", task_id))
            return json.dumps(
                {
                    "success": True,
                    "analysis": json.dumps(
                        {"verdict": "PASS", "reason": "preview renders correctly"}
                    ),
                }
            )

        def cleanup(task_id: str) -> None:
            calls.append(("cleanup", task_id))

        with (
            mock.patch.object(vvp, "visual_validation_enabled", return_value=True),
            mock.patch.object(vvp, "resolve_preview_url", return_value=PREVIEW),
            mock.patch.object(vvp, "browser_navigate", side_effect=navigate),
            mock.patch.object(vvp, "browser_vision", side_effect=vision),
            mock.patch.object(vvp, "cleanup_browser", side_effect=cleanup),
        ):
            result = vvp.run(repo=vvp.PILOT_REPO, pr=412, head=HEAD)

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual([name for name, _ in calls], ["navigate", "vision", "cleanup"])
        navigate_url, task_id = calls[0][1].split("|", 1)
        self.assertEqual(navigate_url, PREVIEW)
        self.assertEqual(calls[1][1], task_id)
        self.assertEqual(calls[2][1], task_id)

    def test_visible_block_is_high_and_cleans_up(self) -> None:
        result, cleanup = self._run_with_tools(
            vision=json.dumps(
                {
                    "success": True,
                    "analysis": json.dumps(
                        {
                            "verdict": "BLOCK",
                            "reason": "hero image is visibly broken",
                        }
                    ),
                }
            )
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["findings"][0]["severity"], "high")
        self.assertIn("hero image", result["findings"][0]["detail"])
        cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
