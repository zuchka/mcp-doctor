from mcp_doctor.analyzers import analyze
from mcp_doctor.models import ServerInspection, ToolDefinition


def _tool(name: str, description: str | None = None, parameter_count: int = 1) -> ToolDefinition:
    properties = {f"field_{index}": {"type": "string"} for index in range(parameter_count)}
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties},
    )


def test_analyzers_find_description_naming_complexity_and_crud_family() -> None:
    inspection = ServerInspection(
        target="test",
        tools=[
            _tool("get_customer", "GET customer endpoint", parameter_count=11),
            _tool("create_customer", "Create a customer record for the support workflow."),
            _tool("delete_customer"),
            _tool("listCustomers", "List customers that match the supplied support filters."),
        ],
    )

    report = analyze(inspection)
    failed_codes = {check.code for check in report.checks if not check.passed}

    assert "parameter-complexity" in failed_codes
    assert "description-quality" in failed_codes
    assert "naming-consistency" in failed_codes
    assert "crud-wrapper-smells" in failed_codes
    assert report.warning_count >= 4


def test_small_task_oriented_surface_passes() -> None:
    inspection = ServerInspection(
        target="test",
        tools=[
            _tool(
                "investigate_customer",
                (
                    "Gather the customer, recent orders, and payment state "
                    "for a support investigation."
                ),
                parameter_count=2,
            ),
            _tool(
                "refund_order",
                "Refund a confirmed order and return the resulting payment status.",
                parameter_count=2,
            ),
        ],
    )

    report = analyze(inspection)

    assert all(check.passed for check in report.checks)
    assert report.warning_count == 0
    assert report.metrics["tool_count"] == 2
