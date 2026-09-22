from mcp_doctor.analyzers import analyze
from mcp_doctor.diff import compare_reports
from mcp_doctor.models import ServerInspection, ToolDefinition
from mcp_doctor.report import render_diff_json, render_diff_text, render_json, render_text


def test_report_includes_inventory_and_estimate_disclaimer() -> None:
    inspection = ServerInspection(
        target="https://example.test/mcp",
        server_name="Example",
        protocol_version="2026-07-28",
        tools=[
            ToolDefinition(
                name="find_order",
                description="Find the order that best matches the supplied customer evidence.",
                input_schema={
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                },
            )
        ],
    )
    report = analyze(inspection)

    text = render_text(report)
    payload = render_json(report)

    assert "MCP Doctor — Inspection Report" in text
    assert "~" in text
    assert "not model-specific" in text
    assert '"tool_count": 1' in payload
    assert '"warning_count"' in payload


def test_report_renders_pair_findings_and_diff() -> None:
    baseline = analyze(ServerInspection(target="test"))
    current = analyze(
        ServerInspection(
            target="test",
            tools=[
                ToolDefinition(
                    name="find_orders",
                    description=(
                        "Find matching orders using the supplied free text and customer details."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                ),
                ToolDefinition(
                    name="search_orders",
                    description=(
                        "Search matching orders using the supplied free text and customer details."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                ),
            ],
        )
    )

    report_text = render_text(current)
    diff = compare_reports(baseline, current)

    assert "find_orders ↔ search_orders" in report_text
    assert "Shared concepts:" in report_text
    assert "New findings" in render_diff_text(diff)
    diff_json = render_diff_json(diff)
    assert '"new_findings"' in diff_json
    assert '"new_warning_count"' in diff_json
