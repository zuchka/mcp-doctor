from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mcp_doctor.evals.models import CapabilityMap, EvalSuite
from mcp_doctor.models import ServerInspection


class EvalConfigError(ValueError):
    """Raised when an eval suite or capability map is invalid."""


def _load_toml(path: Path, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("rb") as stream:
            payload = tomllib.load(stream)
    except OSError as exc:
        raise EvalConfigError(f"Could not read {label} {resolved}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise EvalConfigError(f"Invalid TOML in {label} {resolved}: {exc}") from exc
    if "version" not in payload:
        raise EvalConfigError(f"Invalid {label} {resolved}: missing required 'version'.")
    return payload


def load_eval_suite(path: Path) -> EvalSuite:
    resolved = path.expanduser().resolve()
    try:
        return EvalSuite.model_validate(_load_toml(path, "eval suite"))
    except ValidationError as exc:
        raise EvalConfigError(f"Invalid eval suite {resolved}: {exc}") from exc


def load_capability_map(path: Path) -> CapabilityMap:
    resolved = path.expanduser().resolve()
    try:
        return CapabilityMap.model_validate(_load_toml(path, "capability map"))
    except ValidationError as exc:
        raise EvalConfigError(f"Invalid capability map {resolved}: {exc}") from exc


def fingerprint(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def interface_fingerprint(inspection: ServerInspection) -> str:
    payload = {
        "instructions": inspection.instructions,
        "tools": [
            tool.model_dump(mode="json", exclude_none=True)
            for tool in sorted(inspection.tools, key=lambda item: item.name)
        ],
    }
    return fingerprint(payload)


def validate_capability_map(
    suite: EvalSuite, capability_map: CapabilityMap, inspection: ServerInspection
) -> None:
    problems = []
    if capability_map.suite != suite.name:
        problems.append(
            f"capability map targets suite {capability_map.suite!r}, not {suite.name!r}"
        )
    known_capabilities = {item.id for item in suite.capabilities}
    mapped_capabilities = {
        capability
        for mapping in capability_map.tools.values()
        for capability in mapping.capabilities
    }
    unknown = mapped_capabilities - known_capabilities
    if unknown:
        problems.append("map references unknown capabilities: " + ", ".join(sorted(unknown)))

    live_tools = {tool.name for tool in inspection.tools}
    mapped_tools = set(capability_map.tools)
    missing = live_tools - mapped_tools
    stale = mapped_tools - live_tools
    if missing:
        problems.append("unmapped server tools: " + ", ".join(sorted(missing)))
    if stale:
        problems.append("mapped tools not exposed by server: " + ", ".join(sorted(stale)))

    expected = {capability for task in suite.tasks for capability in task.expected_capabilities}
    unavailable = expected - mapped_capabilities
    if unavailable:
        problems.append("expected capabilities unavailable: " + ", ".join(sorted(unavailable)))
    if problems:
        raise EvalConfigError("Capability-map preflight failed: " + "; ".join(problems))


def filter_suite(
    suite: EvalSuite, *, task_ids: set[str] | None = None, tags: set[str] | None = None
) -> EvalSuite:
    selected = []
    known_task_ids = {task.id for task in suite.tasks}
    if task_ids:
        unknown = task_ids - known_task_ids
        if unknown:
            raise EvalConfigError("Unknown task IDs: " + ", ".join(sorted(unknown)))
    for task in suite.tasks:
        if task_ids and task.id not in task_ids:
            continue
        if tags and not (set(task.tags) & tags):
            continue
        selected.append(task)
    if not selected:
        raise EvalConfigError("Task filters selected no eval tasks.")
    return suite.model_copy(update={"tasks": tuple(selected)})
