import asyncio
import json

from fastmcp import FastMCP

from mcp_doctor.evals.drivers import ScriptedAgentDriver
from mcp_doctor.evals.harness import evaluate_suite, preflight_eval
from mcp_doctor.evals.models import (
    AgentConfig,
    AgentTurn,
    CapabilityDefinition,
    CapabilityMap,
    Effect,
    EvalSuite,
    EvalTask,
    GraderDefinition,
    HarnessConfig,
    RecordingMode,
    ToolCallStatus,
    ToolCapabilityMapping,
    ToolRequest,
)


def _server() -> FastMCP:
    server = FastMCP("Eval Test")

    @server.tool
    def lookup_customer(email: str) -> dict[str, str]:
        """Resolve an email to a customer."""
        return {"customer_id": "C-42", "email": email}

    @server.tool
    def lookup_orders(customer_id: str) -> list[dict[str, str]]:
        """Retrieve orders for a customer."""
        return [{"order_id": "A-123", "status": "shipped", "customer_id": customer_id}]

    @server.tool
    def add_note(customer_id: str, note: str) -> str:
        """Persist a customer note."""
        return f"saved {customer_id}: {note}"

    return server


def _suite(task: EvalTask) -> EvalSuite:
    return EvalSuite(
        version=1,
        name="crm",
        capabilities=(
            CapabilityDefinition(id="customer.lookup", description="Resolve customers."),
            CapabilityDefinition(id="orders.lookup", description="Retrieve orders."),
            CapabilityDefinition(id="customer.note.write", description="Write customer notes."),
        ),
        tasks=(task,),
    )


def _mapping() -> CapabilityMap:
    return CapabilityMap(
        version=1,
        suite="crm",
        revision="v1",
        tools={
            "lookup_customer": ToolCapabilityMapping(
                capabilities=("customer.lookup",), effect=Effect.READ
            ),
            "lookup_orders": ToolCapabilityMapping(
                capabilities=("orders.lookup",), effect=Effect.READ
            ),
            "add_note": ToolCapabilityMapping(
                capabilities=("customer.note.write",), effect=Effect.WRITE
            ),
        },
    )


def test_runs_scripted_agent_and_collects_metrics() -> None:
    task = EvalTask(
        id="order-status",
        prompt="Find the latest order for ada@example.com",
        expected_capabilities=("customer.lookup", "orders.lookup"),
        forbidden_capabilities=("customer.note.write",),
        expected_sequence=("customer.lookup", "orders.lookup"),
        graders=(GraderDefinition(type="final_contains", values=("A-123", "shipped")),),
    )
    driver = ScriptedAgentDriver(
        {
            task.id: [
                AgentTurn(
                    tool_calls=[
                        ToolRequest(
                            call_id="call-1",
                            name="lookup_customer",
                            arguments={"email": "ada@example.com"},
                        )
                    ],
                    latency_ms=5,
                ),
                AgentTurn(
                    tool_calls=[
                        ToolRequest(
                            call_id="call-2",
                            name="lookup_orders",
                            arguments={"customer_id": "C-42"},
                        )
                    ],
                    latency_ms=4,
                ),
                AgentTurn(text="Order A-123 is shipped.", latency_ms=3),
            ]
        }
    )

    run = asyncio.run(
        evaluate_suite(
            _server(),
            target_label="in-memory",
            suite=_suite(task),
            capability_map=_mapping(),
            driver=driver,
            agent_config=AgentConfig(provider="scripted", model="test"),
            harness_config=HarnessConfig(recording=RecordingMode.FULL),
        )
    )

    assert run.metrics["success_rate"] == 1.0
    assert run.metrics["tool_calls"] == 2
    assert run.metrics["irrelevant_calls"] == 0
    assert run.attempts[0].status.value == "passed"
    assert run.attempts[0].tool_calls[0].arguments == {"email": "ada@example.com"}
    assert run.attempts[0].tool_calls[1].result is not None


def test_blocks_write_tools_by_default_and_metadata_hides_payloads() -> None:
    task = EvalTask(
        id="write-note",
        prompt="Save a note",
        expected_capabilities=("customer.note.write",),
    )
    driver = ScriptedAgentDriver(
        {
            task.id: [
                AgentTurn(
                    tool_calls=[
                        ToolRequest(
                            call_id="call-1",
                            name="add_note",
                            arguments={"customer_id": "C-42", "note": "secret"},
                        )
                    ]
                ),
                AgentTurn(text="I could not save it."),
            ]
        }
    )

    run = asyncio.run(
        evaluate_suite(
            _server(),
            target_label="in-memory",
            suite=_suite(task),
            capability_map=_mapping(),
            driver=driver,
            agent_config=AgentConfig(provider="scripted", model="test"),
            harness_config=HarnessConfig(),
        )
    )

    call = run.attempts[0].tool_calls[0]
    assert call.status == ToolCallStatus.BLOCKED
    assert call.arguments is None
    assert call.result is None
    assert not run.attempts[0].success


def test_explicitly_allows_writes_and_redacts_recorded_payloads() -> None:
    task = EvalTask(
        id="write-note",
        prompt="Save a note",
        expected_capabilities=("customer.note.write",),
    )
    driver = ScriptedAgentDriver(
        {
            task.id: [
                AgentTurn(
                    tool_calls=[
                        ToolRequest(
                            call_id="call-1",
                            name="add_note",
                            arguments={"customer_id": "C-42", "note": "secret"},
                        )
                    ]
                ),
                AgentTurn(text="The note was saved."),
            ]
        }
    )

    run = asyncio.run(
        evaluate_suite(
            _server(),
            target_label="in-memory",
            suite=_suite(task),
            capability_map=_mapping(),
            driver=driver,
            agent_config=AgentConfig(provider="scripted", model="test"),
            harness_config=HarnessConfig(
                allowed_effects=(Effect.READ, Effect.WRITE),
                recording=RecordingMode.REDACTED,
                redaction_patterns=("secret",),
            ),
        )
    )

    call = run.attempts[0].tool_calls[0]
    assert call.status == ToolCallStatus.SUCCEEDED
    assert call.arguments == {"customer_id": "C-42", "note": "[REDACTED]"}
    assert "secret" not in json.dumps(call.result)
    assert run.attempts[0].success


def test_preflight_does_not_call_server_tools() -> None:
    server = _server()
    task = EvalTask(
        id="lookup",
        prompt="Look up a customer",
        expected_capabilities=("customer.lookup",),
    )

    result = asyncio.run(
        preflight_eval(
            server,
            target_label="in-memory",
            suite=_suite(task),
            capability_map=_mapping(),
        )
    )

    assert result.tools == 3
    assert result.task_ids == ["lookup"]
