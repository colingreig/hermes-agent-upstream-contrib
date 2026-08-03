"""Pure materialization contract for governed fleet cron jobs."""
from __future__ import annotations

import json
from typing import Any


class FleetJobPayloadError(ValueError):
    """The governed jobs payload cannot be materialized safely."""


def materialize_jobs_payload(payload_bytes: bytes) -> dict[str, Any]:
    """Parse verified source bytes and apply declared prompt postconditions."""
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetJobPayloadError(f"bundled jobs.json is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise FleetJobPayloadError("bundled jobs.json has no top-level 'jobs' list — refusing")

    postconditions = payload.pop("job_prompt_postconditions", {})
    if not isinstance(postconditions, dict):
        raise FleetJobPayloadError("job_prompt_postconditions must be an object keyed by job id")
    jobs_by_id = {
        str(job.get("id")): job for job in payload["jobs"] if isinstance(job, dict)
    }
    for job_id, postcondition in postconditions.items():
        job = jobs_by_id.get(str(job_id))
        if job is None:
            raise FleetJobPayloadError(
                f"prompt postcondition targets missing job id {job_id!r}"
            )
        if not isinstance(postcondition, str) or not postcondition.strip():
            raise FleetJobPayloadError(f"prompt postcondition for job {job_id!r} is empty")
        prompt = job.get("prompt")
        if not isinstance(prompt, str):
            raise FleetJobPayloadError(
                f"prompt postcondition target {job_id!r} has no string prompt"
            )
        job["prompt"] = prompt.rstrip() + "\n\n" + postcondition.strip()
    return payload
