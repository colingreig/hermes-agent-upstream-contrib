#!/usr/bin/env python3
"""Render the human production-writer index from its JSON source of truth.

The registry is intentionally the only authored inventory.  This script keeps
``fleet-config/PRODUCTION_WRITERS.md`` reviewable without creating a second,
manually maintained list of production writers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY = ROOT / "production_mutation_registry.json"
DEFAULT_OUTPUT = ROOT / "fleet-config" / "PRODUCTION_WRITERS.md"
ACTOR_CLASSES = {
    "cron",
    "gateway",
    "conductor-ssh",
    "ignite-email-infra-deploy",
    "dashboard",
}
MUTABILITY = {"read-only", "mutating"}


class RegistryError(ValueError):
    """The checked-in production mutation registry is not self-consistent."""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{field} must be a non-empty string")
    return value


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise RegistryError(f"{field} must be a {'possibly empty ' if allow_empty else ''}non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise RegistryError(f"{field} must contain only non-empty strings")
    if len(value) != len(set(value)):
        raise RegistryError(f"{field} must not contain duplicate values")
    return value


def validate_registry(registry: Any) -> dict[str, Any]:
    """Validate invariants which JSON Schema alone cannot express portably."""
    data = _mapping(registry, "registry")
    if data.get("schema_version") != "1.0":
        raise RegistryError("schema_version must be '1.0'")
    if data.get("registry_kind") != "hermes-production-mutation-registry":
        raise RegistryError("registry_kind is not the production mutation registry")

    policy = _mapping(data.get("unknown_path_policy"), "unknown_path_policy")
    if policy.get("default") != "fail-closed":
        raise RegistryError("unknown_path_policy.default must be fail-closed")
    _string(policy.get("rule"), "unknown_path_policy.rule")
    _string(policy.get("enforcement_status"), "unknown_path_policy.enforcement_status")
    hooks = policy.get("follow_on_hook_points")
    if not isinstance(hooks, list) or not hooks:
        raise RegistryError("unknown_path_policy.follow_on_hook_points must be non-empty")
    for index, hook in enumerate(hooks):
        item = _mapping(hook, f"follow_on_hook_points[{index}]")
        for field in ("task", "hook", "purpose"):
            _string(item.get(field), f"follow_on_hook_points[{index}].{field}")

    resources = data.get("resources")
    if not isinstance(resources, list) or not resources:
        raise RegistryError("resources must be a non-empty list")
    resource_ids: set[str] = set()
    for index, resource in enumerate(resources):
        item = _mapping(resource, f"resources[{index}]")
        resource_id = _string(item.get("id"), f"resources[{index}].id")
        if resource_id in resource_ids:
            raise RegistryError(f"duplicate resource id: {resource_id}")
        resource_ids.add(resource_id)
        for field in ("path", "description", "concurrency_model"):
            _string(item.get(field), f"resources[{index}].{field}")

    actors = data.get("actors")
    if not isinstance(actors, list) or not actors:
        raise RegistryError("actors must be a non-empty list")
    actor_ids: set[str] = set()
    cron_ids: set[str] = set()
    for index, actor in enumerate(actors):
        item = _mapping(actor, f"actors[{index}]")
        actor_id = _string(item.get("id"), f"actors[{index}].id")
        if actor_id in actor_ids:
            raise RegistryError(f"duplicate actor id: {actor_id}")
        actor_ids.add(actor_id)
        _string(item.get("name"), f"actors[{index}].name")
        _string(item.get("owning_repository"), f"actors[{index}].owning_repository")
        if item.get("actor_class") not in ACTOR_CLASSES:
            raise RegistryError(f"actors[{index}].actor_class is invalid")
        if item.get("mutability") not in MUTABILITY:
            raise RegistryError(f"actors[{index}].mutability is invalid")
        _string_list(item.get("entry_points"), f"actors[{index}].entry_points")
        _string_list(
            item.get("resources_touched"), f"actors[{index}].resources_touched", allow_empty=True
        )
        unknown_resources = set(item["resources_touched"]) - resource_ids
        if unknown_resources:
            raise RegistryError(f"actors[{index}] references unknown resources: {sorted(unknown_resources)}")

        boundary = _mapping(
            item.get("current_lock_or_transaction_boundary"),
            f"actors[{index}].current_lock_or_transaction_boundary",
        )
        for field in ("kind", "path_or_identity", "behavior"):
            _string(boundary.get(field), f"actors[{index}].current_lock_or_transaction_boundary.{field}")
        rollback = _mapping(item.get("rollback_surface"), f"actors[{index}].rollback_surface")
        for field in ("surface", "procedure"):
            _string(rollback.get(field), f"actors[{index}].rollback_surface.{field}")
        pointers = item.get("operational_pointers")
        if pointers is not None:
            pointer_map = _mapping(pointers, f"actors[{index}].operational_pointers")
            if set(pointer_map) != {"activation", "rollback", "verification"}:
                raise RegistryError(f"actors[{index}].operational_pointers must name activation, rollback, and verification")
            for name, pointer in pointer_map.items():
                pointer_item = _mapping(pointer, f"actors[{index}].operational_pointers.{name}")
                for field in ("entry_point", "command"):
                    _string(pointer_item.get(field), f"actors[{index}].operational_pointers.{name}.{field}")

        job_ids = _string_list(item.get("cron_job_ids", []), f"actors[{index}].cron_job_ids", allow_empty=True)
        if item["actor_class"] == "cron" and not job_ids:
            raise RegistryError(f"cron actor {actor_id} must name cron_job_ids")
        duplicate_cron_ids = cron_ids.intersection(job_ids)
        if duplicate_cron_ids:
            raise RegistryError(f"cron job IDs are owned by multiple actors: {sorted(duplicate_cron_ids)}")
        cron_ids.update(job_ids)

    return data


def load_registry(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"could not read registry {path}: {exc}") from exc
    return validate_registry(data)


def _cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>")


def render(registry: dict[str, Any]) -> str:
    """Render all human-visible inventory data directly from ``registry``."""
    lines = [
        "<!-- Generated by machine-setup/render_production_writers.py; DO NOT EDIT. -->",
        "# Production writers",
        "",
        "This is a generated index of the machine-readable "
        "[`production_mutation_registry.json`](../production_mutation_registry.json). "
        "Edit that registry, then run `python3 machine-setup/render_production_writers.py`; "
        "CI checks that this file is current.",
        "",
        "## Unknown-path guard",
        "",
        f"**Default:** `{registry['unknown_path_policy']['default']}`. "
        f"{registry['unknown_path_policy']['rule']}",
        "",
        f"**Status:** {registry['unknown_path_policy']['enforcement_status']}",
        "",
        "| Follow-on task | Hook point | Purpose |",
        "| --- | --- | --- |",
    ]
    for hook in registry["unknown_path_policy"]["follow_on_hook_points"]:
        lines.append(f"| `{_cell(hook['task'])}` | {_cell(hook['hook'])} | {_cell(hook['purpose'])} |")

    lines.extend(["", "## Resources", "", "| ID | Path | Concurrency model | Description |", "| --- | --- | --- | --- |"])
    for resource in registry["resources"]:
        lines.append(
            f"| `{_cell(resource['id'])}` | `{_cell(resource['path'])}` | "
            f"{_cell(resource['concurrency_model'])} | {_cell(resource['description'])} |"
        )

    lines.extend([
        "",
        "## Actors",
        "",
        "| Actor | Class | Owner | Access | Governed entry points | Resources | Lock or transaction boundary | Rollback surface | Operational pointers |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for actor in registry["actors"]:
        entry_points = "<br>".join(f"`{_cell(point)}`" for point in actor["entry_points"])
        legacy = actor.get("legacy_entry_point_names", [])
        if legacy:
            entry_points += "<br>Legacy name: " + ", ".join(f"`{_cell(name)}`" for name in legacy)
        cron_ids = actor.get("cron_job_ids", [])
        if cron_ids:
            entry_points += "<br>Cron IDs: " + ", ".join(f"`{_cell(job)}`" for job in cron_ids)
        if actor.get("logical_dispatch_surface"):
            entry_points += f"<br>Logical dispatch: `{_cell(actor['logical_dispatch_surface'])}`"
        boundary = actor["current_lock_or_transaction_boundary"]
        rollback = actor["rollback_surface"]
        pointers = actor.get("operational_pointers")
        pointer_text = "—"
        if pointers:
            pointer_text = "<br>".join(
                f"{name.title()}: `{_cell(pointer['entry_point'])}` — `{_cell(pointer['command'])}`"
                for name, pointer in pointers.items()
            )
        lines.append(
            f"| `{_cell(actor['id'])}` — {_cell(actor['name'])} | `{actor['actor_class']}` | "
            f"`{_cell(actor['owning_repository'])}` | `{actor['mutability']}` | {entry_points} | "
            f"{', '.join(f'`{_cell(resource)}`' for resource in actor['resources_touched']) or '—'} | "
            f"{_cell(boundary['kind'])}: `{_cell(boundary['path_or_identity'])}`. {_cell(boundary['behavior'])} | "
            f"`{_cell(rollback['surface'])}`. {_cell(rollback['procedure'])} | {pointer_text} |"
        )
    notes = [actor for actor in registry["actors"] if actor.get("notes")]
    if notes:
        lines.extend(["", "## Notes", ""])
        for actor in notes:
            lines.append(f"- **{actor['name']}:** {actor['notes']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if the generated index is stale")
    args = parser.parse_args(argv)
    try:
        rendered = render(load_registry(args.registry))
    except RegistryError as exc:
        print(f"production mutation registry invalid: {exc}", file=sys.stderr)
        return 2
    if args.check:
        try:
            existing = args.output.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"production writers index unavailable: {exc}", file=sys.stderr)
            return 1
        if existing != rendered:
            print("production writers index is stale; run machine-setup/render_production_writers.py", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
