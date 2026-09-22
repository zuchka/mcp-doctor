from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp_doctor.lab.io import sha256
from mcp_doctor.lab.models import (
    AttemptStatus,
    EvalsContract,
    LabAttempt,
    LabError,
    LabPlan,
    PlannedAttempt,
    Usage,
)


class ResultError(LabError):
    def __init__(self, category: AttemptStatus, message: str):
        self.category = category
        super().__init__(message)


def _required(value: dict[str, Any], key: str, expected_type: type) -> Any:
    item = value.get(key)
    if type(item) is not expected_type:
        raise ResultError(
            AttemptStatus.SCHEMA_ERROR, f"Result requires {key}: {expected_type.__name__}."
        )
    return item


def _optional_number(value: dict[str, Any], key: str) -> int | float | None:
    item = value.get(key)
    if item is None:
        return None
    if type(item) not in (int, float) or item < 0:
        raise ResultError(AttemptStatus.SCHEMA_ERROR, f"Result {key} must be a nonnegative number.")
    return item


def _optional_int(value: dict[str, Any], key: str) -> int | None:
    item = value.get(key)
    if item is None:
        return None
    if type(item) is not int or item < 0:
        raise ResultError(
            AttemptStatus.SCHEMA_ERROR, f"Result {key} must be a nonnegative integer."
        )
    return item


def import_result(
    raw: bytes, *, path: Path, plan: LabPlan, attempt: PlannedAttempt, contract: EvalsContract
) -> LabAttempt:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultError(
            AttemptStatus.CORRUPT_RESULT, f"Invalid result JSON {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ResultError(AttemptStatus.CORRUPT_RESULT, "Result must be a JSON object.")
    provenance = _required(data, "scopeLab", dict)
    if (
        type(provenance.get("version")) is not int
        or provenance["version"] != contract.result_schema_version
    ):
        raise ResultError(
            AttemptStatus.SCHEMA_ERROR,
            "Unsupported scopeLab result version; update the pinned Evals contract and importer.",
        )
    condition = next(item for item in plan.manifest.conditions if item.id == attempt.condition_id)
    expected = {
        "experiment": attempt.experiment,
        "eval": attempt.task_id,
        "interface": "mcp",
    }
    for key, value in expected.items():
        if data.get(key) != value:
            raise ResultError(
                AttemptStatus.PROVENANCE_MISMATCH, f"Result {key} differs from planned {value!r}."
            )
    if (
        type(data.get("run")) is not int
        or data["run"] != attempt.repetition - 1 + plan.run_index_base
    ):
        raise ResultError(AttemptStatus.PROVENANCE_MISMATCH, "Result run index differs from plan.")
    display = _required(data, "experimentDisplay", dict)
    _required(display, "modelProvider", str)
    matching = next(item for item in contract.experiments if item.id == attempt.experiment)
    expected_display = {
        "modelId": plan.manifest.agent.model,
        "agent": plan.manifest.agent.harness,
        "reasoningEffort": plan.manifest.agent.reasoning_effort,
    }
    for key, value in expected_display.items():
        if display.get(key) != value:
            raise ResultError(
                AttemptStatus.PROVENANCE_MISMATCH,
                f"Result experimentDisplay.{key} differs from plan.",
            )
    for key, value in {
        "configSha256": matching.config_sha256,
        "mcpServerVersion": plan.manifest.sources.mcp_server_version,
    }.items():
        if provenance.get(key) != value:
            raise ResultError(
                AttemptStatus.PROVENANCE_MISMATCH,
                f"Result scopeLab.{key} differs from plan.",
            )
    features = provenance.get("features")
    if (
        not isinstance(features, list)
        or not all(isinstance(item, str) for item in features)
        or len(features) != len(set(features))
        or set(features) != set(matching.features)
    ):
        raise ResultError(
            AttemptStatus.PROVENANCE_MISMATCH,
            "Runtime MCP features differ from the planned condition.",
        )
    installed_skills = provenance.get("skills")
    if (
        not isinstance(installed_skills, list)
        or not all(isinstance(item, str) for item in installed_skills)
        or len(installed_skills) != len(set(installed_skills))
        or set(installed_skills) != set(matching.skills)
    ):
        raise ResultError(
            AttemptStatus.PROVENANCE_MISMATCH,
            "Runtime preinstalled skills differ from the planned condition.",
        )
    if provenance.get("runtime") != plan.manifest.runtime.model_dump(mode="json"):
        raise ResultError(
            AttemptStatus.PROVENANCE_MISMATCH,
            "Runtime sandbox settings differ from the plan.",
        )
    sandbox_id = provenance.get("sandboxId")
    if not isinstance(sandbox_id, str) or not sandbox_id.strip():
        raise ResultError(
            AttemptStatus.PROVENANCE_MISMATCH,
            "Result lacks a unique ephemeral sandbox ID.",
        )
    passed = _required(data, "passed", bool)
    checks_raw = data.get("checks", [])
    if not isinstance(checks_raw, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or type(item.get("passed")) is not bool
        for item in checks_raw
    ):
        raise ResultError(
            AttemptStatus.SCHEMA_ERROR, "Result checks must be named pass/fail entries."
        )
    checks = {item["name"]: item["passed"] for item in checks_raw}
    if len(checks) != len(checks_raw):
        raise ResultError(AttemptStatus.SCHEMA_ERROR, "Result has duplicate check names.")
    skills = _required(data, "skills", dict)
    available, loaded = skills.get("available"), skills.get("loaded")
    if not isinstance(available, list) or not all(isinstance(item, str) for item in available):
        raise ResultError(
            AttemptStatus.SCHEMA_ERROR, "Result skills.available must be a string list."
        )
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise ResultError(AttemptStatus.SCHEMA_ERROR, "Result skills.loaded must be a string list.")
    if not set(loaded).issubset(set(available)):
        raise ResultError(AttemptStatus.SCHEMA_ERROR, "Loaded skills must also be available.")
    supabase_available = {item for item in available if item.startswith("supabase")}
    expected_skills = set(plan.manifest.skill_levels[condition.skill_level].skills)
    if supabase_available != expected_skills:
        raise ResultError(
            AttemptStatus.PROVENANCE_MISMATCH,
            "Available Supabase skills differ from the condition.",
        )
    usage_raw = data.get("usage", [])
    if not isinstance(usage_raw, list):
        raise ResultError(AttemptStatus.SCHEMA_ERROR, "Result usage must be an array by model.")
    usage = {}
    for item in usage_raw:
        if not isinstance(item, dict) or not isinstance(item.get("model"), str):
            raise ResultError(
                AttemptStatus.SCHEMA_ERROR, "Result usage entries must be model objects."
            )
        model = item["model"]
        if model in usage:
            raise ResultError(AttemptStatus.SCHEMA_ERROR, f"Duplicate usage model {model!r}.")
        try:
            cache_read = _required(item, "cacheReadInputTokens", int)
            cache_write = _required(item, "cacheWriteInputTokens", int)
            usage[model] = Usage.model_validate(
                {
                    "input_tokens": _required(item, "inputTokens", int),
                    "cached_input_tokens": cache_read + cache_write,
                    "cache_read_input_tokens": cache_read,
                    "cache_write_input_tokens": cache_write,
                    "output_tokens": _required(item, "outputTokens", int),
                }
            )
        except ValueError as exc:
            raise ResultError(
                AttemptStatus.SCHEMA_ERROR, f"Invalid model usage for {model}: {exc}"
            ) from exc
    roots = (Path(plan.manifest.sources.evals_checkout), Path(plan.output_dir))
    relative = None
    for root in roots:
        try:
            relative = str(path.resolve().relative_to(root.resolve()))
            break
        except ValueError:
            continue
    if relative is None:
        raise ResultError(AttemptStatus.PROVENANCE_MISMATCH, "Result is outside the planned roots.")
    docs = data.get("docs")
    if docs is not None and (not isinstance(docs, dict) or not isinstance(docs.get("calls"), list)):
        raise ResultError(
            AttemptStatus.SCHEMA_ERROR, "Result docs.calls must be an array when present."
        )
    docs_metadata: dict[str, int | float | str | bool] = {}
    sources = {"search_docs", "web_fetch", "web_search", "shell_fetch"}
    content_count = 0
    result_chars = 0
    for call in docs["calls"] if docs is not None else []:
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("source"), str)
            or call["source"] not in sources
        ):
            raise ResultError(AttemptStatus.SCHEMA_ERROR, "Invalid docs call source.")
        if not isinstance(call.get("query"), str) or not isinstance(call.get("pages"), list):
            raise ResultError(AttemptStatus.SCHEMA_ERROR, "Invalid docs call metadata.")
        if "hasContent" in call and type(call["hasContent"]) is not bool:
            raise ResultError(AttemptStatus.SCHEMA_ERROR, "Invalid docs hasContent flag.")
        source = call["source"]
        key = f"source_{source}"
        docs_metadata[key] = int(docs_metadata.get(key, 0)) + 1
        if call.get("hasContent") is True:
            content_count += 1
        chars = call.get("resultChars")
        if chars is not None:
            if type(chars) not in (int, float) or chars < 0:
                raise ResultError(AttemptStatus.SCHEMA_ERROR, "Invalid docs resultChars.")
            result_chars += chars
    if docs is not None:
        docs_metadata["with_content"] = content_count
        docs_metadata["result_chars"] = result_chars
    return LabAttempt(
        study_id=plan.manifest.id,
        task_id=attempt.task_id,
        repetition=attempt.repetition,
        condition_id=condition.id,
        experiment=attempt.experiment,
        sandbox_id=sandbox_id,
        status=AttemptStatus.PASSED if passed else AttemptStatus.FAILED,
        passed=passed,
        checks=checks,
        skills_available=tuple(available),
        skills_loaded=tuple(loaded),
        docs_call_count=len(docs["calls"]) if docs is not None else None,
        docs_call_metadata=docs_metadata,
        step_count=_optional_int(data, "stepCount"),
        tool_call_count=_optional_int(data, "toolCallCount"),
        duration_ms=_optional_number(data, "agentRunDurationMs"),
        usage_by_model=usage,
        raw_result_path=relative,
        raw_result_sha256=sha256(raw),
    )
