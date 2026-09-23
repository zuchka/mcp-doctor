"""Symmetric comparison of two saved, intentional MCP tool surfaces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mcp_doctor.analyzers import _parameter_stats
from mcp_doctor.diff import load_report
from mcp_doctor.models import AnalysisReport, Finding, ToolDefinition


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


class ComparisonError(ValueError):
    """A surface comparison input or output is invalid."""


class Side(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    path: str
    report_sha256: str
    target: str
    server_name: str | None
    server_version: str | None
    analysis_version: str
    policy_version: int | None
    policy_sha256: str
    tool_count: int
    definition_bytes: int | float | None
    definition_tokens: int | float | None
    schema_complexity: dict[str, int | float]
    finding_counts: dict[str, int]
    listing_errors: dict[str, str]


class FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    field: str
    a_sha256: str
    b_sha256: str


class NumericComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    a: int | float
    b: int | float
    b_minus_a: int | float


class FindingSides(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprint: str
    a: Finding | None = None
    b: Finding | None = None


class ComparabilityWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class SurfaceComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    comparison_format_version: int = Field(default=1, ge=1, le=1)
    a: Side
    b: Side
    tool_presence_known: bool
    valid_for_lab: bool
    only_a_tools: list[str]
    only_b_tools: list[str]
    shared_tools: list[str]
    changed_fields: list[FieldChange]
    changed_meta: list[FieldChange]
    metrics: list[NumericComparison]
    only_a_findings: list[FindingSides]
    only_b_findings: list[FindingSides]
    shared_findings: list[FindingSides]
    warnings: list[ComparabilityWarning]


def _side(label: str, path: Path, raw: bytes, report: AnalysisReport) -> Side:
    metrics = report.metrics
    findings = [finding for check in report.checks for finding in check.findings]
    schema_stats = [_parameter_stats(tool) for tool in report.inspection.tools]
    complexity = {
        "total_top_level_parameters": sum(item[0] for item in schema_stats),
        "total_required_parameters": sum(item[1] for item in schema_stats),
        "max_schema_depth": max((item[2] for item in schema_stats), default=0),
        "max_union_branches": max((item[3] for item in schema_stats), default=0),
    }
    complexity.update(
        {
            key: value
            for key, value in metrics.items()
            if "parameter" in key or "schema" in key or "union" in key
        }
    )
    return Side(
        label=label,
        path=str(path),
        report_sha256=hashlib.sha256(raw).hexdigest(),
        target=report.inspection.target,
        server_name=report.inspection.server_name,
        server_version=report.inspection.server_version,
        analysis_version=report.analysis_version,
        policy_version=report.policy.get("version"),
        policy_sha256=_digest(report.policy),
        tool_count=len(report.inspection.tools),
        definition_bytes=metrics.get("total_definition_bytes"),
        definition_tokens=metrics.get("estimated_definition_tokens"),
        schema_complexity=complexity,
        finding_counts={
            "total": len(findings),
            "warning": report.warning_count,
            "info": report.info_count,
            "suppressed": report.suppressed_count,
        },
        listing_errors=report.inspection.listing_errors,
    )


def _findings(report: AnalysisReport) -> dict[str, Finding]:
    return {item.fingerprint: item for check in report.checks for item in check.findings}


def _fields(tool: ToolDefinition) -> dict[str, Any]:
    return tool.model_dump(mode="json")


def compare_surfaces(
    report_a: AnalysisReport,
    report_b: AnalysisReport,
    *,
    path_a: Path,
    path_b: Path,
    raw_a: bytes,
    raw_b: bytes,
    label_a: str = "A",
    label_b: str = "B",
) -> SurfaceComparison:
    """Compare reports without reconnecting; paths are provenance only."""
    if not label_a.strip() or not label_b.strip():
        raise ComparisonError("Comparison labels must be nonblank.")
    a = _side(label_a, path_a, raw_a, report_a)
    b = _side(label_b, path_b, raw_b, report_b)
    warnings: list[ComparabilityWarning] = []
    if a.analysis_version != b.analysis_version:
        warnings.append(
            ComparabilityWarning(code="analysis_version", message="Analysis versions differ.")
        )
    if a.policy_sha256 != b.policy_sha256:
        warnings.append(ComparabilityWarning(code="policy", message="Analysis policies differ."))
    for key in sorted(set(report_a.metrics) ^ set(report_b.metrics)):
        warnings.append(
            ComparabilityWarning(
                code="missing_metric", message=f"Metric {key!r} is absent on one side."
            )
        )
    presence_known = "tools" not in a.listing_errors and "tools" not in b.listing_errors
    if not presence_known:
        warnings.append(
            ComparabilityWarning(
                code="tool_listing_error",
                message="A tool listing failed; tool absence cannot be inferred.",
            )
        )
    tools_a = {tool.name: tool for tool in report_a.inspection.tools}
    tools_b = {tool.name: tool for tool in report_b.inspection.tools}
    if len(tools_a) != len(report_a.inspection.tools) or len(tools_b) != len(
        report_b.inspection.tools
    ):
        warnings.append(
            ComparabilityWarning(
                code="duplicate_tool_name", message="A report contains duplicate tool names."
            )
        )
        presence_known = False
    shared = sorted(set(tools_a) & set(tools_b))
    changes: list[FieldChange] = []
    meta_changes: list[FieldChange] = []
    for name in shared:
        values_a, values_b = _fields(tools_a[name]), _fields(tools_b[name])
        for field in (
            "title",
            "description",
            "input_schema",
            "output_schema",
            "annotations",
            "meta",
        ):
            if values_a[field] != values_b[field]:
                change = FieldChange(
                    tool=name,
                    field=field,
                    a_sha256=_digest(values_a[field]),
                    b_sha256=_digest(values_b[field]),
                )
                (meta_changes if field == "meta" else changes).append(change)
    findings_a, findings_b = _findings(report_a), _findings(report_b)
    keys_a, keys_b = set(findings_a), set(findings_b)
    return SurfaceComparison(
        a=a,
        b=b,
        tool_presence_known=presence_known,
        valid_for_lab=presence_known,
        only_a_tools=sorted(set(tools_a) - set(tools_b)) if presence_known else [],
        only_b_tools=sorted(set(tools_b) - set(tools_a)) if presence_known else [],
        shared_tools=shared,
        changed_fields=changes,
        changed_meta=meta_changes,
        metrics=[
            NumericComparison(
                name=key,
                a=report_a.metrics[key],
                b=report_b.metrics[key],
                b_minus_a=report_b.metrics[key] - report_a.metrics[key],
            )
            for key in sorted(set(report_a.metrics) & set(report_b.metrics))
        ],
        only_a_findings=[
            FindingSides(fingerprint=key, a=findings_a[key]) for key in sorted(keys_a - keys_b)
        ],
        only_b_findings=[
            FindingSides(fingerprint=key, b=findings_b[key]) for key in sorted(keys_b - keys_a)
        ],
        shared_findings=[
            FindingSides(fingerprint=key, a=findings_a[key], b=findings_b[key])
            for key in sorted(keys_a & keys_b)
        ],
        warnings=warnings,
    )


def compare_saved_surfaces(
    path_a: Path, path_b: Path, *, label_a: str = "A", label_b: str = "B"
) -> SurfaceComparison:
    a, b = path_a.expanduser().resolve(), path_b.expanduser().resolve()
    report_a, report_b = load_report(a), load_report(b)
    return compare_surfaces(
        report_a,
        report_b,
        path_a=a,
        path_b=b,
        raw_a=a.read_bytes(),
        raw_b=b.read_bytes(),
        label_a=label_a,
        label_b=label_b,
    )


def render_comparison(comparison: SurfaceComparison) -> str:
    lines = [
        "MCP Doctor — Surface Comparison",
        f"{comparison.a.label}: {comparison.a.tool_count} tools, "
        f"{comparison.a.definition_tokens} estimated tokens",
        f"{comparison.b.label}: {comparison.b.tool_count} tools, "
        f"{comparison.b.definition_tokens} estimated tokens",
        f"Shared: {len(comparison.shared_tools)}; "
        f"only {comparison.a.label}: {len(comparison.only_a_tools)}; "
        f"only {comparison.b.label}: {len(comparison.only_b_tools)}",
    ]
    for item in comparison.metrics:
        lines.append(f"{item.name}: {item.a} | {item.b} | B − A = {item.b_minus_a:+}")
    for item in comparison.changed_fields:
        lines.append(
            f"Changed {item.tool}.{item.field}: {item.a_sha256[:12]} | {item.b_sha256[:12]}"
        )
    for item in comparison.warnings:
        lines.append(f"Warning [{item.code}]: {item.message}")
    return "\n".join(lines)
