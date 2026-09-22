from __future__ import annotations

import json

from mcp_doctor.models import AnalysisReport


def render_json(report: AnalysisReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)


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
    for check in report.checks:
        status = "PASS" if check.passed else "WARN"
        lines.append(f"[{status}] {check.title}: {check.summary}")
        for finding in check.findings:
            prefix = f"{finding.tool}: " if finding.tool else ""
            lines.append(f"  - {prefix}{finding.message}")
            if finding.suggestion:
                lines.append(f"    Suggestion: {finding.suggestion}")

    lines.extend(
        [
            "",
            f"Result: {report.warning_count} warning{'s' if report.warning_count != 1 else ''}.",
            (
                "Token counts are estimates (compact JSON bytes ÷ 4), "
                "not model-specific billing counts."
            ),
        ]
    )
    return "\n".join(lines)
