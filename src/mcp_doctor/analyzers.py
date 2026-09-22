from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from typing import Any

from mcp_doctor.models import (
    AnalysisReport,
    CheckResult,
    Finding,
    ServerInspection,
    Severity,
    ToolDefinition,
)

TOOL_COUNT_WARNING = 20
TOTAL_TOKEN_WARNING = 8_000
TOOL_TOKEN_WARNING = 2_000
PARAMETER_COUNT_WARNING = 10
SCHEMA_DEPTH_WARNING = 4
UNION_BRANCH_WARNING = 5

CRUD_VERBS = {"create", "get", "list", "update", "delete", "patch", "put", "fetch"}
HTTP_LANGUAGE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b|\b(endpoint|API route)\b", re.I)
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
CAMEL_CASE = re.compile(r"^[a-z][A-Za-z0-9]*$")
KEBAB_CASE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def _definition_payload(tool: ToolDefinition) -> dict[str, Any]:
    return tool.model_dump(mode="json", exclude_none=True)


def _compact_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _estimated_tokens(byte_count: int) -> int:
    # Four bytes per token is a common English/JSON planning estimate, not a tokenizer claim.
    return math.ceil(byte_count / 4)


def _schema_depth(value: Any, depth: int = 0) -> int:
    if isinstance(value, dict):
        structural_children = [
            child
            for key, child in value.items()
            if key in {"properties", "items", "anyOf", "oneOf", "allOf", "$defs", "definitions"}
        ]
        if not structural_children:
            return depth
        return max(_schema_depth(child, depth + 1) for child in structural_children)
    if isinstance(value, list) and value:
        return max(_schema_depth(child, depth) for child in value)
    return depth


def _union_branches(value: Any) -> int:
    if isinstance(value, dict):
        local = max(
            (len(value.get(key, [])) for key in ("anyOf", "oneOf") if key in value),
            default=0,
        )
        return max(local, *(_union_branches(child) for child in value.values()))
    if isinstance(value, list):
        return max((_union_branches(child) for child in value), default=0)
    return 0


def _parameter_stats(tool: ToolDefinition) -> tuple[int, int, int, int]:
    schema = tool.input_schema
    properties = schema.get("properties", {})
    property_count = len(properties) if isinstance(properties, dict) else 0
    required = schema.get("required", [])
    required_count = len(required) if isinstance(required, list) else 0
    return property_count, required_count, _schema_depth(schema), _union_branches(schema)


def _name_style(name: str) -> str:
    if "_" in name and SNAKE_CASE.fullmatch(name):
        return "snake_case"
    if "-" in name and KEBAB_CASE.fullmatch(name):
        return "kebab-case"
    if CAMEL_CASE.fullmatch(name):
        return "camelCase" if any(char.isupper() for char in name) else "single-word"
    return "nonstandard"


def _name_words(name: str) -> list[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ").replace("-", " ")
    return [word.lower() for word in separated.split() if word]


def analyze(inspection: ServerInspection) -> AnalysisReport:
    tool_sizes = {tool.name: _compact_size(_definition_payload(tool)) for tool in inspection.tools}
    total_bytes = sum(tool_sizes.values())
    parameter_counts = [_parameter_stats(tool)[0] for tool in inspection.tools]
    metrics: dict[str, int | float] = {
        "tool_count": len(inspection.tools),
        "total_definition_bytes": total_bytes,
        "estimated_definition_tokens": _estimated_tokens(total_bytes),
        "total_parameters": sum(parameter_counts),
        "average_parameters_per_tool": (
            round(sum(parameter_counts) / len(parameter_counts), 1) if parameter_counts else 0.0
        ),
    }

    checks = [
        _check_tool_count(inspection),
        _check_schema_footprint(inspection, tool_sizes, total_bytes),
        _check_parameter_complexity(inspection),
        _check_descriptions(inspection),
        _check_names(inspection),
        _check_crud_smells(inspection),
    ]
    return AnalysisReport(inspection=inspection, metrics=metrics, checks=checks)


def _check_tool_count(inspection: ServerInspection) -> CheckResult:
    count = len(inspection.tools)
    findings = []
    if count > TOOL_COUNT_WARNING:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                message=f"The server exposes {count} tools in one catalog.",
                suggestion="Consider task-oriented tools or smaller, scoped server surfaces.",
            )
        )
    summary = (
        f"{count} tool{'s' if count != 1 else ''}; "
        f"warning threshold is >{TOOL_COUNT_WARNING}."
    )
    return CheckResult(
        code="tool-count",
        title="Tool count",
        passed=not findings,
        summary=summary,
        findings=findings,
    )


def _check_schema_footprint(
    inspection: ServerInspection, tool_sizes: dict[str, int], total_bytes: int
) -> CheckResult:
    findings = []
    total_tokens = _estimated_tokens(total_bytes)
    if total_tokens > TOTAL_TOKEN_WARNING:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                message=f"All tool definitions occupy about {total_tokens:,} tokens.",
                suggestion="Shorten descriptions or expose a smaller task-specific tool set.",
            )
        )
    for tool in inspection.tools:
        tokens = _estimated_tokens(tool_sizes[tool.name])
        if tokens > TOOL_TOKEN_WARNING:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    tool=tool.name,
                    message=f"Definition is about {tokens:,} tokens.",
                    suggestion=(
                        "Simplify nested schemas and remove context that does not guide tool use."
                    ),
                )
            )
    return CheckResult(
        code="schema-footprint",
        title="Schema footprint",
        passed=not findings,
        summary=(
            f"{total_bytes:,} bytes, approximately {total_tokens:,} tokens "
            "across tool definitions."
        ),
        findings=findings,
    )


def _check_parameter_complexity(inspection: ServerInspection) -> CheckResult:
    findings = []
    for tool in inspection.tools:
        count, required, depth, union_branches = _parameter_stats(tool)
        problems = []
        if count > PARAMETER_COUNT_WARNING:
            problems.append(f"{count} top-level parameters")
        if depth > SCHEMA_DEPTH_WARNING:
            problems.append(f"schema depth {depth}")
        if union_branches > UNION_BRANCH_WARNING:
            problems.append(f"{union_branches} alternatives in one union")
        if problems:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    tool=tool.name,
                    message=f"Complex input: {', '.join(problems)} ({required} required).",
                    suggestion=(
                        "Split distinct workflows or replace low-level options "
                        "with intent-level inputs."
                    ),
                )
            )
    return CheckResult(
        code="parameter-complexity",
        title="Parameter complexity",
        passed=not findings,
        summary=(
            f"{sum(_parameter_stats(tool)[0] for tool in inspection.tools)} top-level parameters "
            "across all tools."
        ),
        findings=findings,
    )


def _check_descriptions(inspection: ServerInspection) -> CheckResult:
    findings = []
    for tool in inspection.tools:
        description = (tool.description or "").strip()
        if not description:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    tool=tool.name,
                    message="Description is missing.",
                    suggestion="Explain what the tool does and when an agent should choose it.",
                )
            )
        elif len(description) < 30:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    tool=tool.name,
                    message=f"Description is only {len(description)} characters.",
                    suggestion=(
                        "Add selection guidance, important constraints, and the outcome returned."
                    ),
                )
            )
        elif len(description) > 800:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    tool=tool.name,
                    message=f"Description is {len(description)} characters long.",
                    suggestion=(
                        "Move background material elsewhere and keep agent-facing guidance focused."
                    ),
                )
            )
    described = len(inspection.tools) - sum(
        1 for tool in inspection.tools if not (tool.description or "").strip()
    )
    return CheckResult(
        code="description-quality",
        title="Description quality",
        passed=not findings,
        summary=f"{described}/{len(inspection.tools)} tools have descriptions.",
        findings=findings,
    )


def _check_names(inspection: ServerInspection) -> CheckResult:
    styles = {tool.name: _name_style(tool.name) for tool in inspection.tools}
    findings = []
    for name, style in styles.items():
        if style == "nonstandard":
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    tool=name,
                    message=(
                        "Name does not follow a recognizable lower-case or camelCase convention."
                    ),
                    suggestion="Use one predictable convention, preferably verb_noun snake_case.",
                )
            )
    recognized = {style for style in styles.values() if style not in {"nonstandard", "single-word"}}
    if len(recognized) > 1:
        counts = Counter(styles.values())
        findings.append(
            Finding(
                severity=Severity.WARNING,
                message="Multiple naming conventions are mixed: "
                + ", ".join(f"{style} ({count})" for style, count in sorted(counts.items())),
                suggestion="Choose one naming convention across the server.",
            )
        )
    return CheckResult(
        code="naming-consistency",
        title="Naming consistency",
        passed=not findings,
        summary=(
            ", ".join(
                f"{style}: {count}"
                for style, count in sorted(Counter(styles.values()).items())
            )
            if styles
            else "No tool names to inspect."
        ),
        findings=findings,
    )


def _check_crud_smells(inspection: ServerInspection) -> CheckResult:
    findings = []
    entity_verbs: dict[str, set[str]] = defaultdict(set)
    crud_named_tools = 0
    for tool in inspection.tools:
        words = _name_words(tool.name)
        if words and words[0] in CRUD_VERBS:
            crud_named_tools += 1
            entity = "_".join(words[1:]) or "<unknown>"
            entity_verbs[entity].add(words[0])
        if tool.description and HTTP_LANGUAGE.search(tool.description):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    tool=tool.name,
                    message="Description reads like a direct HTTP/API operation.",
                    suggestion=(
                        "Describe the user outcome and agent decision boundary, not the endpoint."
                    ),
                )
            )
    for entity, verbs in sorted(entity_verbs.items()):
        if len(verbs) >= 3:
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    message=f"Entity {entity!r} exposes a CRUD family: {', '.join(sorted(verbs))}.",
                    suggestion=(
                        "Test whether one or two task-oriented tools can hide this orchestration."
                    ),
                )
            )
    count = len(inspection.tools)
    if count >= 6 and crud_named_tools / count >= 0.7:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                message=f"{crud_named_tools}/{count} tool names begin with a generic CRUD verb.",
                suggestion="Design around agent tasks instead of mirroring every API operation.",
            )
        )
    return CheckResult(
        code="crud-wrapper-smells",
        title="CRUD/API-wrapper smells",
        passed=not findings,
        summary=f"{crud_named_tools}/{len(inspection.tools)} tools use generic CRUD-style names.",
        findings=findings,
    )
