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
from mcp_doctor.policy import AnalysisPolicy, Thresholds, apply_policy
from mcp_doctor.semantic import (
    PairSimilarity,
    has_negative_guidance,
    has_positive_guidance,
    overlapping_pairs,
    pair_has_selection_boundary,
)

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


def analyze(inspection: ServerInspection, policy: AnalysisPolicy | None = None) -> AnalysisReport:
    policy = policy or AnalysisPolicy()
    thresholds = policy.thresholds
    tool_sizes = {tool.name: _compact_size(_definition_payload(tool)) for tool in inspection.tools}
    total_bytes = sum(tool_sizes.values())
    parameter_counts = [_parameter_stats(tool)[0] for tool in inspection.tools]
    pairs = overlapping_pairs(inspection.tools, thresholds.semantic_overlap)
    metrics: dict[str, int | float] = {
        "tool_count": len(inspection.tools),
        "total_definition_bytes": total_bytes,
        "estimated_definition_tokens": _estimated_tokens(total_bytes),
        "total_parameters": sum(parameter_counts),
        "average_parameters_per_tool": (
            round(sum(parameter_counts) / len(parameter_counts), 1) if parameter_counts else 0.0
        ),
        "overlapping_tool_pairs": len(pairs),
    }

    checks = [
        _check_tool_count(inspection, thresholds),
        _check_schema_footprint(inspection, tool_sizes, total_bytes, thresholds),
        _check_parameter_complexity(inspection, thresholds),
        _check_descriptions(inspection, thresholds),
        _check_selection_guidance(inspection),
        _check_semantic_overlap(inspection, pairs, thresholds.semantic_overlap),
        _check_names(inspection),
        _check_crud_smells(inspection),
    ]
    checks = apply_policy(checks, policy)
    return AnalysisReport(
        inspection=inspection,
        metrics=metrics,
        checks=checks,
        policy=policy.model_dump(mode="json"),
    )


def _check_tool_count(inspection: ServerInspection, thresholds: Thresholds) -> CheckResult:
    count = len(inspection.tools)
    findings = []
    if count > thresholds.tool_count:
        findings.append(
            Finding(
                rule_id="tool-count.too-many",
                severity=Severity.WARNING,
                subject="server",
                message=f"The server exposes {count} tools in one catalog.",
                suggestion="Consider task-oriented tools or smaller, scoped server surfaces.",
                evidence={"actual": count, "threshold": thresholds.tool_count},
            )
        )
    summary = (
        f"{count} tool{'s' if count != 1 else ''}; warning threshold is >{thresholds.tool_count}."
    )
    return CheckResult(
        code="tool-count",
        title="Tool count",
        passed=not findings,
        summary=summary,
        findings=findings,
    )


def _check_schema_footprint(
    inspection: ServerInspection,
    tool_sizes: dict[str, int],
    total_bytes: int,
    thresholds: Thresholds,
) -> CheckResult:
    findings = []
    total_tokens = _estimated_tokens(total_bytes)
    if total_tokens > thresholds.total_definition_tokens:
        findings.append(
            Finding(
                rule_id="schema-footprint.total",
                severity=Severity.WARNING,
                subject="server",
                message=f"All tool definitions occupy about {total_tokens:,} tokens.",
                suggestion="Shorten descriptions or expose a smaller task-specific tool set.",
                evidence={
                    "actual_tokens": total_tokens,
                    "threshold": thresholds.total_definition_tokens,
                },
            )
        )
    for tool in inspection.tools:
        tokens = _estimated_tokens(tool_sizes[tool.name])
        if tokens > thresholds.tool_definition_tokens:
            findings.append(
                Finding(
                    rule_id="schema-footprint.tool",
                    severity=Severity.WARNING,
                    tools=[tool.name],
                    message=f"Definition is about {tokens:,} tokens.",
                    suggestion=(
                        "Simplify nested schemas and remove context that does not guide tool use."
                    ),
                    evidence={
                        "actual_tokens": tokens,
                        "threshold": thresholds.tool_definition_tokens,
                    },
                )
            )
    return CheckResult(
        code="schema-footprint",
        title="Schema footprint",
        passed=not findings,
        summary=(
            f"{total_bytes:,} bytes, approximately {total_tokens:,} tokens across tool definitions."
        ),
        findings=findings,
    )


def _check_parameter_complexity(
    inspection: ServerInspection, thresholds: Thresholds
) -> CheckResult:
    findings = []
    for tool in inspection.tools:
        count, required, depth, union_branches = _parameter_stats(tool)
        problems = []
        if count > thresholds.parameter_count:
            problems.append(f"{count} top-level parameters")
        if depth > thresholds.schema_depth:
            problems.append(f"schema depth {depth}")
        if union_branches > thresholds.union_branches:
            problems.append(f"{union_branches} alternatives in one union")
        if problems:
            findings.append(
                Finding(
                    rule_id="parameter-complexity.tool",
                    severity=Severity.WARNING,
                    tools=[tool.name],
                    message=f"Complex input: {', '.join(problems)} ({required} required).",
                    suggestion=(
                        "Split distinct workflows or replace low-level options "
                        "with intent-level inputs."
                    ),
                    evidence={
                        "parameters": count,
                        "required": required,
                        "schema_depth": depth,
                        "union_branches": union_branches,
                    },
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


def _check_descriptions(inspection: ServerInspection, thresholds: Thresholds) -> CheckResult:
    findings = []
    for tool in inspection.tools:
        description = (tool.description or "").strip()
        if not description:
            findings.append(
                Finding(
                    rule_id="description-quality.missing",
                    severity=Severity.WARNING,
                    tools=[tool.name],
                    message="Description is missing.",
                    suggestion="Explain what the tool does and when an agent should choose it.",
                )
            )
        elif len(description) < thresholds.description_min_chars:
            findings.append(
                Finding(
                    rule_id="description-quality.short",
                    severity=Severity.WARNING,
                    tools=[tool.name],
                    message=f"Description is only {len(description)} characters.",
                    suggestion=(
                        "Add selection guidance, important constraints, and the outcome returned."
                    ),
                    evidence={
                        "actual_chars": len(description),
                        "threshold": thresholds.description_min_chars,
                    },
                )
            )
        elif len(description) > thresholds.description_max_chars:
            findings.append(
                Finding(
                    rule_id="description-quality.long",
                    severity=Severity.WARNING,
                    tools=[tool.name],
                    message=f"Description is {len(description)} characters long.",
                    suggestion=(
                        "Move background material elsewhere and keep agent-facing guidance focused."
                    ),
                    evidence={
                        "actual_chars": len(description),
                        "threshold": thresholds.description_max_chars,
                    },
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


def _check_selection_guidance(inspection: ServerInspection) -> CheckResult:
    findings = []
    guided = 0
    for tool in inspection.tools:
        description = (tool.description or "").strip()
        if not description:
            continue
        if has_positive_guidance(description):
            guided += 1
            continue
        findings.append(
            Finding(
                rule_id="selection-guidance.missing-positive",
                severity=Severity.WARNING,
                tools=[tool.name],
                message="Description lacks explicit positive selection guidance.",
                suggestion=(
                    "State when an agent should use this tool, for example with 'Use when…'."
                ),
            )
        )
    return CheckResult(
        code="selection-guidance",
        title="Selection guidance",
        passed=not findings,
        summary=f"{guided}/{len(inspection.tools)} tools include explicit positive guidance.",
        findings=findings,
    )


def _check_semantic_overlap(
    inspection: ServerInspection, pairs: list[PairSimilarity], threshold: float
) -> CheckResult:
    tools = {tool.name: tool for tool in inspection.tools}
    findings = []
    ambiguous = 0
    for pair in pairs:
        left = tools[pair.left]
        right = tools[pair.right]
        has_boundary = pair_has_selection_boundary(left, right)
        if not has_boundary:
            ambiguous += 1
        evidence = {
            "score": pair.score,
            "threshold": threshold,
            "name_score": pair.name_score,
            "description_score": pair.description_score,
            "parameter_score": pair.parameter_score,
            "matched_terms": list(pair.matched_terms),
            "shared_parameters": list(pair.shared_parameters),
            "positive_guidance": {
                pair.left: has_positive_guidance(left.description),
                pair.right: has_positive_guidance(right.description),
            },
            "negative_guidance": {
                pair.left: has_negative_guidance(left.description),
                pair.right: has_negative_guidance(right.description),
            },
        }
        if has_boundary:
            findings.append(
                Finding(
                    rule_id="semantic-overlap.guided-pair",
                    severity=Severity.INFO,
                    tools=[pair.left, pair.right],
                    message=(
                        f"Definitions overlap with score {pair.score:.2f}, but their descriptions "
                        "provide explicit selection boundaries."
                    ),
                    evidence=evidence,
                )
            )
        else:
            findings.append(
                Finding(
                    rule_id="semantic-overlap.ambiguous-pair",
                    severity=Severity.WARNING,
                    tools=[pair.left, pair.right],
                    message=(
                        f"Definitions overlap with score {pair.score:.2f} and may be ambiguous."
                    ),
                    suggestion=(
                        "Differentiate when each tool should be used and add explicit contrasting "
                        "guidance such as 'Do not use for…'."
                    ),
                    evidence=evidence,
                )
            )
    return CheckResult(
        code="semantic-overlap",
        title="Semantic overlap",
        passed=ambiguous == 0,
        summary=(
            f"{len(pairs)} pair{'s' if len(pairs) != 1 else ''} met the {threshold:.2f} "
            f"overlap threshold; {ambiguous} ambiguous."
        ),
        findings=findings,
    )


def _check_names(inspection: ServerInspection) -> CheckResult:
    styles = {tool.name: _name_style(tool.name) for tool in inspection.tools}
    findings = []
    for name, style in styles.items():
        if style == "nonstandard":
            findings.append(
                Finding(
                    rule_id="naming-consistency.nonstandard",
                    severity=Severity.WARNING,
                    tools=[name],
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
                rule_id="naming-consistency.mixed",
                severity=Severity.WARNING,
                subject="server",
                message="Multiple naming conventions are mixed: "
                + ", ".join(f"{style} ({count})" for style, count in sorted(counts.items())),
                suggestion="Choose one naming convention across the server.",
                evidence={"styles": dict(sorted(counts.items()))},
            )
        )
    return CheckResult(
        code="naming-consistency",
        title="Naming consistency",
        passed=not findings,
        summary=(
            ", ".join(
                f"{style}: {count}" for style, count in sorted(Counter(styles.values()).items())
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
                    rule_id="crud-wrapper-smells.http-language",
                    severity=Severity.WARNING,
                    tools=[tool.name],
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
                    rule_id="crud-wrapper-smells.family",
                    severity=Severity.WARNING,
                    subject=entity,
                    message=f"Entity {entity!r} exposes a CRUD family: {', '.join(sorted(verbs))}.",
                    suggestion=(
                        "Test whether one or two task-oriented tools can hide this orchestration."
                    ),
                    evidence={"verbs": sorted(verbs)},
                )
            )
    count = len(inspection.tools)
    if count >= 6 and crud_named_tools / count >= 0.7:
        findings.append(
            Finding(
                rule_id="crud-wrapper-smells.ratio",
                severity=Severity.WARNING,
                subject="server",
                message=f"{crud_named_tools}/{count} tool names begin with a generic CRUD verb.",
                suggestion="Design around agent tasks instead of mirroring every API operation.",
                evidence={"crud_named_tools": crud_named_tools, "tool_count": count},
            )
        )
    return CheckResult(
        code="crud-wrapper-smells",
        title="CRUD/API-wrapper smells",
        passed=not findings,
        summary=f"{crud_named_tools}/{len(inspection.tools)} tools use generic CRUD-style names.",
        findings=findings,
    )
