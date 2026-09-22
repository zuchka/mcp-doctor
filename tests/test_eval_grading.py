from mcp_doctor.evals.grading import grade_attempt
from mcp_doctor.evals.models import (
    EvalTask,
    GraderDefinition,
    ToolCallStatus,
    ToolCallTrace,
)


def test_grades_capability_sequence_and_final_answer() -> None:
    task = EvalTask(
        id="order-status",
        prompt="Find the order",
        expected_capabilities=("customer.lookup", "orders.lookup"),
        expected_sequence=("customer.lookup", "orders.lookup"),
        forbidden_capabilities=("customer.write",),
        graders=(
            GraderDefinition(type="final_contains", values=("A-123", "shipped")),
            GraderDefinition(
                type="structured_result",
                tool="lookup_orders",
                path="structuredContent.result.0.status",
                equals="shipped",
            ),
        ),
    )
    calls = [
        ToolCallTrace(
            turn=1,
            call_id="1",
            tool="lookup_customer",
            capabilities=["customer.lookup"],
            relevant=True,
            status=ToolCallStatus.SUCCEEDED,
        ),
        ToolCallTrace(
            turn=2,
            call_id="2",
            tool="lookup_orders",
            capabilities=["orders.lookup"],
            relevant=True,
            status=ToolCallStatus.SUCCEEDED,
            result={"structuredContent": {"result": [{"status": "shipped"}]}},
        ),
    ]

    results = grade_attempt(
        task, final_answer="Order A-123 has shipped.", tool_calls=calls, max_tool_calls=4
    )

    assert all(result.passed for result in results if result.required)


def test_forbidden_call_fails_required_grader() -> None:
    task = EvalTask(
        id="read-only",
        prompt="Read it",
        expected_capabilities=("thing.read",),
        forbidden_capabilities=("thing.write",),
    )
    call = ToolCallTrace(
        turn=1,
        call_id="1",
        tool="write_thing",
        capabilities=["thing.write"],
        forbidden=True,
        status=ToolCallStatus.BLOCKED,
    )

    results = grade_attempt(task, final_answer="", tool_calls=[call], max_tool_calls=1)

    assert not next(item for item in results if item.grader_id == "forbidden-capabilities").passed
    assert not next(item for item in results if item.grader_id == "tool-execution").passed
