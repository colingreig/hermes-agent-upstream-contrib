#!/usr/bin/env python3
"""Fail-closed deployed-preview visual validation for the jdmbuysell pilot.

This module deliberately adds no browser or model plumbing.  It drives the
existing Hermes path:

    configured Browserbase -> browser_navigate -> browser_vision
                           -> call_llm(task="vision")

The rollout is source-gated to one repository and defaults off in behavioral
configuration. Outside that exact repository, or while disabled, ``run`` is a
no-op. The target repository does not currently publish GitHub Deployments, so
the rollout also requires a separate, approved preview producer. That producer
must publish the head-bound check-run contract below; until it exists, enabled
validation deliberately fails closed with ``missing-preview``.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit


PILOT_REPO = "colingreig/jdmbuysell-v4"
DEFAULT_CONFIG = Path("~/.hermes/config.yaml").expanduser()
GH_TIMEOUT = 60
CHECK_NAME = "visual-preview"
PREVIEW_CHECK_NAME = "Hermes PR Preview"
PREVIEW_CHECK_APP_ID = 4053083  # Hermes Dev Assistant GitHub App
PREVIEW_MARKER = "HERMES_VISUAL_PREVIEW_V1 "
_TASK_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# These are control-plane/detail destinations, not rendered customer previews.
_CONTROL_PLANE_HOSTS = frozenset(
    {
        "github.com",
        "www.github.com",
        "vercel.com",
        "www.vercel.com",
        "app.vercel.com",
        "dashboard.render.com",
        "app.netlify.com",
        "dash.cloudflare.com",
        "dashboard.cloudflare.com",
    }
)
_CONTROL_PLANE_PATH_SEGMENTS = frozenset({"dashboard", "deployments", "details"})

_VISION_PROMPT = """Review this deployed PR preview as a strict visual release gate.
Inspect the rendered page, not source code. Check for broken or missing images,
obvious layout overflow/overlap, unreadable text, empty/error states, failed
assets, and visibly incomplete content. Do not block on subjective taste.

Return ONLY one JSON object, with no markdown or prose:
{"verdict":"PASS","reason":"one concise observed reason"}
or
{"verdict":"BLOCK","reason":"one concise, concrete visible defect"}"""


class VisualPreviewError(RuntimeError):
    """An enabled pilot validation could not produce a trustworthy verdict."""


def _normalized_result(
    *,
    status: str,
    verdict: str,
    reason: str,
    preview_url: str = "",
    findings: list[dict[str, str]] | None = None,
    failure_class: str = "",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "lens": CHECK_NAME,
        "status": status,
        "verdict": verdict,
        "reason": reason,
        "preview_url": preview_url,
        "findings": list(findings or []),
    }
    if failure_class:
        result["failure_class"] = failure_class
    return result


def _skip(status: str, reason: str) -> dict[str, Any]:
    return _normalized_result(
        status=status,
        verdict="SKIP",
        reason=reason,
    )


def operational_failure(
    failure_class: str,
    reason: str,
    *,
    preview_url: str = "",
) -> dict[str, Any]:
    detail = f"{failure_class}: {reason}"
    return _normalized_result(
        status="failed",
        verdict="BLOCK",
        reason=reason,
        preview_url=preview_url,
        failure_class=failure_class,
        findings=[
            {
                "check": CHECK_NAME,
                "severity": "high",
                "file": preview_url or "(deployed preview)",
                "detail": detail,
            }
        ],
    )


def visual_validation_enabled(config_path: Path = DEFAULT_CONFIG) -> bool:
    """Read ``content_pipeline.visual_validation.enabled`` conservatively.

    This is behavioral configuration, so it lives in ``config.yaml``.  Missing
    files, missing PyYAML, malformed YAML, non-mapping shapes, and non-boolean
    values all preserve the source default: disabled.
    """
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        content_pipeline = parsed.get("content_pipeline")
        if not isinstance(content_pipeline, dict):
            return False
        visual_validation = content_pipeline.get("visual_validation")
        if not isinstance(visual_validation, dict):
            return False
        return visual_validation.get("enabled") is True
    except (ImportError, OSError, UnicodeError, AttributeError, TypeError, ValueError):
        return False
    except Exception:
        # PyYAML parser errors inherit from yaml.YAMLError, but importing that
        # type at module load would defeat the optional-dependency behavior.
        return False


def _gh_api_json(endpoint: str) -> Any:
    try:
        completed = subprocess.run(
            ["gh", "api", "-X", "GET", endpoint],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except Exception as exc:
        raise VisualPreviewError(f"GitHub deployment lookup failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown gh error").strip()[:240]
        raise VisualPreviewError(
            f"GitHub deployment lookup failed (rc={completed.returncode}): {detail}"
        )
    try:
        return json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise VisualPreviewError("GitHub deployment lookup returned invalid JSON") from exc


def _eligible_preview_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or hostname in _CONTROL_PLANE_HOSTS
        or hostname.startswith("dashboard.")
    ):
        return ""
    path_segments = {
        segment.lower() for segment in parsed.path.split("/") if segment.strip()
    }
    if path_segments & _CONTROL_PLANE_PATH_SEGMENTS:
        return ""
    # Fragments are browser-local and should not distinguish deployment proof.
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def resolve_preview_url(repo: str, head: str) -> str:
    """Resolve the approved, head-bound preview-producer check-run contract.

    The target repo has no GitHub Deployments API producer. We therefore accept
    only a completed successful check run created by the Hermes Dev Assistant
    GitHub App, with the exact ``Hermes PR Preview`` name, exact ``head_sha``,
    and an output line of this form::

        HERMES_VISUAL_PREVIEW_V1 {"head_sha":"<sha>","url":"https://..."}

    GitHub authenticates the check-run App identity and binds the check run to
    the commit. Repeating the head inside the App-authored marker prevents a
    stale or accidentally copied preview from being accepted. No PR-body,
    ordinary comment, deployment dashboard, main-site fallback, or unapproved
    check output is trusted.
    """
    if not head:
        raise VisualPreviewError("PR head SHA is missing")

    repo_path = quote(repo, safe="/")
    payload = _gh_api_json(
        f"repos/{repo_path}/commits/{quote(head, safe='')}/check-runs?per_page=100"
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
        raise VisualPreviewError("GitHub check-runs response is malformed")

    candidates = []
    for check_run in payload["check_runs"]:
        app = check_run.get("app") if isinstance(check_run, dict) else None
        if (
            not isinstance(check_run, dict)
            or check_run.get("name") != PREVIEW_CHECK_NAME
            or check_run.get("head_sha") != head
            or check_run.get("status") != "completed"
            or check_run.get("conclusion") != "success"
            or not isinstance(app, dict)
            or app.get("id") != PREVIEW_CHECK_APP_ID
        ):
            continue
        candidates.append(check_run)
    candidates.sort(
        key=lambda check_run: (
            str(check_run.get("completed_at") or ""),
            int(check_run.get("id") or 0),
        ),
        reverse=True,
    )

    for check_run in candidates:
        output = check_run.get("output")
        if not isinstance(output, dict):
            continue
        output_text = "\n".join(
            value for value in (output.get("title"), output.get("summary"), output.get("text"))
            if isinstance(value, str)
        )
        for line in output_text.splitlines():
            if not line.startswith(PREVIEW_MARKER):
                continue
            try:
                marker = json.loads(line[len(PREVIEW_MARKER):])
            except json.JSONDecodeError:
                continue
            if not isinstance(marker, dict) or marker.get("head_sha") != head:
                continue
            preview_url = _eligible_preview_url(marker.get("url"))
            if preview_url:
                return preview_url

    raise VisualPreviewError(
        "no approved head-bound preview check exists; provision the separate "
        f"{PREVIEW_CHECK_NAME!r} producer with Hermes Dev Assistant App "
        f"{PREVIEW_CHECK_APP_ID} before enabling this pilot"
    )


def _ensure_runtime_import_path() -> None:
    """Make the installed Hermes runtime importable from the Mini script copy."""
    candidates = [
        Path("~/.hermes/runtime-current").expanduser(),
        Path(__file__).resolve().parents[3],
    ]
    for candidate in candidates:
        if (candidate / "tools" / "browser_tool.py").is_file():
            candidate_text = str(candidate)
            if candidate_text not in sys.path:
                sys.path.insert(0, candidate_text)
            return


def browser_navigate(url: str, *, task_id: str) -> Any:
    _ensure_runtime_import_path()
    from tools.browser_tool import browser_navigate as navigate

    return navigate(url, task_id=task_id)


def browser_vision(question: str, *, task_id: str) -> Any:
    _ensure_runtime_import_path()
    from tools.browser_tool import browser_vision as vision

    # browser_vision owns screenshot capture and routes its auxiliary analysis
    # through call_llm(task="vision").
    return vision(question, annotate=False, task_id=task_id)


def call_auxiliary_vision(content: list[dict[str, Any]]) -> str:
    """Obtain a textual verdict when browser_vision returns a native envelope."""
    _ensure_runtime_import_path()
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    response = call_llm(
        task="vision",
        messages=[{"role": "user", "content": content}],
        max_tokens=1000,
        temperature=0.1,
        timeout=120,
    )
    return extract_content_or_reasoning(response)


def cleanup_browser(task_id: str) -> None:
    _ensure_runtime_import_path()
    from tools.browser_tool import cleanup_browser as cleanup

    cleanup(task_id)


def _tool_payload(raw: Any, stage: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VisualPreviewError(f"{stage} returned invalid JSON") from exc
    else:
        raise VisualPreviewError(f"{stage} returned an unsupported result")
    if not isinstance(payload, dict):
        raise VisualPreviewError(f"{stage} returned a non-object result")
    if payload.get("success") is not True:
        detail = str(payload.get("error") or "success was not true")[:240]
        raise VisualPreviewError(f"{stage} failed: {detail}")
    return payload


def _parse_vision_verdict(analysis: Any) -> tuple[str, str]:
    if not isinstance(analysis, str) or not analysis.strip():
        raise VisualPreviewError("vision analysis was empty")
    text = analysis.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisualPreviewError("vision analysis contained no parseable JSON verdict") from exc
    if not isinstance(payload, dict):
        raise VisualPreviewError("vision verdict was not a JSON object")
    verdict = str(payload.get("verdict") or "").strip().upper()
    reason = str(payload.get("reason") or "").strip()
    if verdict not in {"PASS", "BLOCK"}:
        raise VisualPreviewError("vision verdict must be PASS or BLOCK")
    if not reason:
        raise VisualPreviewError("vision verdict reason was empty")
    return verdict, reason


def _vision_analysis(raw: Any) -> str:
    """Normalize browser_vision's auxiliary JSON and native envelope shapes.

    Native envelopes contain pixels but no verdict. They must not be parsed as
    success/analysis JSON and must not be treated as PASS. Re-route the image
    content through the configured ``auxiliary.vision`` client and require the
    same explicit PASS/BLOCK contract.
    """
    if isinstance(raw, dict) and raw.get("_multimodal") is True:
        content = raw.get("content")
        if not isinstance(content, list):
            raise VisualPreviewError("native vision envelope content is malformed")
        image_parts = [
            part
            for part in content
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        if not image_parts:
            raise VisualPreviewError("native vision envelope contains no screenshot")
        analysis = call_auxiliary_vision(
            [{"type": "text", "text": _VISION_PROMPT}, *image_parts]
        )
        if not isinstance(analysis, str) or not analysis.strip():
            raise VisualPreviewError("auxiliary vision returned no analysis")
        return analysis

    payload = _tool_payload(raw, "browser vision")
    analysis = payload.get("analysis")
    if not isinstance(analysis, str) or not analysis.strip():
        raise VisualPreviewError("browser vision returned no analysis")
    return analysis


def run(
    *,
    repo: str,
    pr: int,
    head: str,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Run the visual preview pilot and return a normalized lens result."""
    if (repo or "").lower() != PILOT_REPO:
        return _skip("not-pilot", f"repository is outside pilot ({PILOT_REPO})")
    if not visual_validation_enabled(config_path):
        return _skip("disabled", "content_pipeline.visual_validation.enabled is not true")

    task_id = _TASK_SAFE_RE.sub(
        "-", f"validator-visual-preview-{repo}-{pr}-{head[:12]}"
    )[:120]
    preview_url = ""
    try:
        try:
            preview_url = resolve_preview_url(repo, head)
        except Exception as exc:
            return operational_failure("missing-preview", str(exc))

        try:
            _tool_payload(
                browser_navigate(preview_url, task_id=task_id),
                "browser navigation",
            )
        except Exception as exc:
            return operational_failure("navigation-failure", str(exc), preview_url=preview_url)

        try:
            analysis = _vision_analysis(
                browser_vision(_VISION_PROMPT, task_id=task_id)
            )
        except Exception as exc:
            return operational_failure("vision-failure", str(exc), preview_url=preview_url)

        try:
            verdict, reason = _parse_vision_verdict(analysis)
        except Exception as exc:
            return operational_failure("parse-failure", str(exc), preview_url=preview_url)

        severity = "info" if verdict == "PASS" else "high"
        return _normalized_result(
            status="passed" if verdict == "PASS" else "blocked",
            verdict=verdict,
            reason=reason,
            preview_url=preview_url,
            findings=[
                {
                    "check": CHECK_NAME,
                    "severity": severity,
                    "file": preview_url,
                    "detail": reason,
                }
            ],
        )
    finally:
        # Reap the Browserbase/agent-browser session after every enabled pilot
        # attempt, including missing-preview, navigation, vision, and parse
        # failures. Cleanup errors must not erase the already normalized result.
        try:
            cleanup_browser(task_id)
        except Exception as exc:
            print(
                f"[visual-preview] browser cleanup failed for {task_id}: {exc!r}",
                file=sys.stderr,
            )
