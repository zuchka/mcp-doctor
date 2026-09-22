from __future__ import annotations

import re
from typing import Any

from mcp_doctor.evals.models import (
    EvalTask,
    GraderDefinition,
    GraderResult,
    ToolCallStatus,
    ToolCallTrace,
)


def _result(
    grader_id: str,
    passed: bool,
    message: str,
    *,
    required: bool = True,
    evidence: dict[str, Any] | None = None,
) -> GraderResult:
    return GraderResult(
        grader_id=grader_id,
        passed=passed,
        required=required,
        message=message,
        evidence=evidence or {},
    )


def _path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return False, None
    return True, current


def _custom_grader(
    definition: GraderDefinition,
    index: int,
    final_answer: str,
    tool_calls: list[ToolCallTrace],
) -> GraderResult:
    grader_id = definition.id or f"{definition.type}:{index}"
    if definition.type == "final_contains":
        haystack = final_answer if definition.case_sensitive else final_answer.casefold()
        missing = [
            value
            for value in definition.values
            if (value if definition.case_sensitive else value.casefold()) not in haystack
        ]
        return _result(
            grader_id,
            not missing,
            "Final answer contains all required values."
            if not missing
            else "Final answer is missing: " + ", ".join(missing),
            required=definition.required,
            evidence={"missing": missing},
        )
    if definition.type == "final_regex":
        flags = 0 if definition.case_sensitive else re.IGNORECASE
        matched = re.search(definition.pattern or "", final_answer, flags) is not None
        return _result(
            grader_id,
            matched,
            "Final answer matched the required pattern."
            if matched
            else "Final answer did not match the required pattern.",
            required=definition.required,
            evidence={"pattern": definition.pattern},
        )

    matching = [
        call
        for call in tool_calls
        if call.tool == definition.tool and call.status == ToolCallStatus.SUCCEEDED
    ]
    found = False
    actual = None
    for call in matching:
        found, actual = _path(call.result, definition.path or "")
        if found and actual == definition.equals:
            break
    passed = found and actual == definition.equals
    return _result(
        grader_id,
        passed,
        "Structured tool result matched." if passed else "Structured tool result did not match.",
        required=definition.required,
        evidence={
            "tool": definition.tool,
            "path": definition.path,
            "expected": definition.equals,
            "actual": actual,
        },
    )


def grade_attempt(
    task: EvalTask,
    *,
    final_answer: str,
    tool_calls: list[ToolCallTrace],
    max_tool_calls: int,
) -> list[GraderResult]:
    successful = [call for call in tool_calls if call.status == ToolCallStatus.SUCCEEDED]
    used_capabilities = {capability for call in successful for capability in call.capabilities}
    missing = set(task.expected_capabilities) - used_capabilities
    results = [
        _result(
            "required-capabilities",
            not missing,
            "All expected capabilities were used."
            if not missing
            else "Missing expected capabilities: " + ", ".join(sorted(missing)),
            evidence={"missing": sorted(missing)},
        )
    ]
    forbidden = [call.tool for call in tool_calls if call.forbidden]
    results.append(
        _result(
            "forbidden-capabilities",
            not forbidden,
            "No forbidden capabilities were attempted."
            if not forbidden
            else "Forbidden tool calls: " + ", ".join(forbidden),
            evidence={"tools": forbidden},
        )
    )
    errors = [
        call.tool
        for call in tool_calls
        if call.status
        in {
            ToolCallStatus.MCP_ERROR,
            ToolCallStatus.INVALID,
            ToolCallStatus.BLOCKED,
            ToolCallStatus.TIMEOUT,
            ToolCallStatus.BUDGET_EXCEEDED,
        }
    ]
    results.append(
        _result(
            "tool-execution",
            not errors,
            "All tool calls succeeded."
            if not errors
            else "Tool-call failures: " + ", ".join(errors),
            evidence={"tools": errors},
        )
    )
    results.append(
        _result(
            "tool-call-budget",
            len(tool_calls) <= max_tool_calls,
            f"Used {len(tool_calls)} of {max_tool_calls} allowed tool calls.",
            evidence={"actual": len(tool_calls), "maximum": max_tool_calls},
        )
    )
    if task.expected_sequence:
        actual = [
            capability
            for call in successful
            for capability in call.capabilities
            if capability in task.expected_sequence
        ]
        expected = list(task.expected_sequence)
        positions = []
        cursor = 0
        for capability in expected:
            try:
                cursor = actual.index(capability, cursor) + 1
                positions.append(cursor - 1)
            except ValueError:
                positions = []
                break
        results.append(
            _result(
                "capability-sequence",
                bool(positions),
                "Expected capability sequence was observed."
                if positions
                else "Expected capability sequence was not observed.",
                evidence={"expected": expected, "actual": actual},
            )
        )
    if not task.expected_capabilities and not task.allowed_capabilities:
        results.append(
            _result(
                "no-tool-needed",
                not tool_calls,
                "The task was answered without tool calls."
                if not tool_calls
                else "The task should not have used tools.",
                evidence={"calls": len(tool_calls)},
            )
        )
    results.extend(
        _custom_grader(definition, index, final_answer, tool_calls)
        for index, definition in enumerate(task.graders, start=1)
    )
    return results
