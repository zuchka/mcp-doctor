from mcp_doctor.analyzers import analyze
from mcp_doctor.models import ServerInspection, ToolDefinition
from mcp_doctor.report import render_json, render_text


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
