from __future__ import annotations

import json

from mcp_doctor.diff import ReportDiff
from mcp_doctor.models import AnalysisReport, Finding
from mcp_doctor.policy import AnalysisPolicy


def render_json(report: AnalysisReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)


def render_diff_json(diff: ReportDiff) -> str:
    return json.dumps(diff.model_dump(mode="json"), indent=2, sort_keys=True)


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def render_text(report: AnalysisReport) -> str:
    inspection = report.inspection
    server_label = inspection.server_name or "<name not provided>"
    if inspection.server_version:
        server_label += f" {inspection.server_version}"

    lines = [
        "MCP Doctor — Inspection Report",
        "=" * 30,
        f"Target:   {inspection.target}",
        f"Server:   {server_label}",
        f"Protocol: {inspection.protocol_version or '<unknown>'}",
        "",
        "Capability inventory",
        "--------------------",
        f"Tools:              {len(inspection.tools)}",
        f"Resources:          {len(inspection.resources)}",
        f"Resource templates: {len(inspection.resource_templates)}",
        f"Prompts:            {len(inspection.prompts)}",
    ]

    if inspection.capabilities:
        capability_names = ", ".join(sorted(inspection.capabilities))
        lines.append(f"Advertised:          {capability_names}")
    for capability, error in inspection.listing_errors.items():
        lines.append(f"Could not list {capability}: {error}")

    inventories = (
        ("Resources", inspection.resources),
        ("Resource templates", inspection.resource_templates),
        ("Prompts", inspection.prompts),
    )
    for title, items in inventories:
        if items:
            lines.extend(["", f"{title}: " + ", ".join(item.name for item in items)])

    lines.extend(["", "Tool surface", "------------"])
    if not inspection.tools:
        lines.append("No tools were returned.")
    else:
        header = f"{'NAME':<34} {'PARAMS':>6} {'REQ':>4}  DESCRIPTION"
        lines.extend([header, "-" * len(header)])
        for tool in inspection.tools:
            properties = tool.input_schema.get("properties", {})
            required = tool.input_schema.get("required", [])
            parameter_count = len(properties) if isinstance(properties, dict) else 0
            required_count = len(required) if isinstance(required, list) else 0
            description = " ".join((tool.description or "<missing>").split())
            lines.append(
                f"{_clip(tool.name, 34):<34} {parameter_count:>6} {required_count:>4}  "
                f"{_clip(description, 66)}"
            )

    lines.extend(
        [
            "",
            "Quality checks",
            "--------------",
            (
                f"Estimated tool-definition footprint: "
                f"{report.metrics['total_definition_bytes']:,} bytes / "
                f"~{report.metrics['estimated_definition_tokens']:,} tokens"
            ),
        ]
    )
    default_policy = AnalysisPolicy().model_dump(mode="json")
    if report.policy and report.policy != default_policy:
        changed_thresholds = sorted(
            key
            for key, value in report.policy.get("thresholds", {}).items()
            if value != default_policy["thresholds"].get(key)
        )
        policy_parts = []
        if changed_thresholds:
            policy_parts.append("thresholds: " + ", ".join(changed_thresholds))
        if report.policy.get("enabled_rules") is not None:
            policy_parts.append("enabled-rule filter")
        if report.policy.get("disabled_rules"):
            policy_parts.append(f"{len(report.policy['disabled_rules'])} disabled rule(s)")
        if report.policy.get("suppressions"):
            policy_parts.append(f"{len(report.policy['suppressions'])} suppression(s)")
        lines.append("Policy: custom (" + "; ".join(policy_parts) + ")")
    for check in report.checks:
        active = [finding for finding in check.findings if not finding.suppressed]
        status = "WARN" if not check.passed else "INFO" if active else "PASS"
        lines.append(f"[{status}] {check.title}: {check.summary}")
        for finding in check.findings:
            prefix = f"{' ↔ '.join(finding.tools)}: " if finding.tools else ""
            marker = "[suppressed] " if finding.suppressed else ""
            lines.append(f"  - [{finding.rule_id}] {marker}{prefix}{finding.message}")
            if finding.suggestion:
                lines.append(f"    Suggestion: {finding.suggestion}")
            if finding.rule_id.startswith("semantic-overlap"):
                matched = finding.evidence.get("matched_terms", [])
                if matched:
                    lines.append(f"    Shared concepts: {', '.join(matched)}")
            if finding.suppression_reason:
                lines.append(f"    Suppression: {finding.suppression_reason}")

    lines.extend(
        [
            "",
            (
                f"Result: {report.warning_count} active warning"
                f"{'s' if report.warning_count != 1 else ''}, {report.info_count} info, "
                f"{report.suppressed_count} suppressed."
            ),
            (
                "Token counts are estimates (compact JSON bytes ÷ 4), "
                "not model-specific billing counts."
            ),
        ]
    )
    return "\n".join(lines)


def _finding_line(finding: Finding) -> str:
    tools = " ↔ ".join(finding.tools)
    prefix = f"{tools}: " if tools else ""
    return f"[{finding.rule_id}] {prefix}{finding.message}"


def render_diff_text(diff: ReportDiff) -> str:
    lines = [
        "MCP Doctor — Report Diff",
        "=" * 24,
        f"Baseline: {diff.baseline_target}",
        f"Current:  {diff.current_target}",
    ]
    if diff.policy_changed:
        lines.extend(
            [
                "",
                (
                    "WARNING: Analysis policy changed; threshold or suppression changes may "
                    "affect this diff."
                ),
            ]
        )
    if diff.analysis_changed:
        lines.extend(
            [
                "",
                (
                    "WARNING: Analyzer version changed from "
                    f"{diff.baseline_analysis_version} to {diff.current_analysis_version}; "
                    "rule behavior may differ."
                ),
            ]
        )
    if diff.added_tools or diff.removed_tools:
        lines.extend(["", "Tool changes", "------------"])
        if diff.added_tools:
            lines.append("Added:   " + ", ".join(diff.added_tools))
        if diff.removed_tools:
            lines.append("Removed: " + ", ".join(diff.removed_tools))
    if diff.metric_deltas:
        lines.extend(["", "Metric changes", "--------------"])
        for metric in diff.metric_deltas:
            sign = "+" if metric.delta > 0 else ""
            lines.append(f"{metric.name}: {metric.before} -> {metric.after} ({sign}{metric.delta})")

    sections = (
        ("New findings", diff.new_findings),
        ("Newly active", diff.newly_active),
        ("Resolved findings", diff.resolved_findings),
        ("Newly suppressed", diff.newly_suppressed),
    )
    for title, findings in sections:
        if findings:
            lines.extend(["", title, "-" * len(title)])
            lines.extend(f"- {_finding_line(finding)}" for finding in findings)

    lines.extend(
        [
            "",
            (
                f"Result: {diff.new_warning_count} new active warning"
                f"{'s' if diff.new_warning_count != 1 else ''}; "
                f"{len(diff.resolved_findings)} resolved; "
                f"{len(diff.unchanged_findings)} unchanged."
            ),
        ]
    )
    return "\n".join(lines)
