import asyncio
from types import SimpleNamespace

from mcp_doctor.evals.drivers import OpenAIResponsesDriver
from mcp_doctor.evals.models import AgentConfig, EvalTask, ToolOutput
from mcp_doctor.models import ToolDefinition


class _Response:
    def __init__(self, payload, output_text="") -> None:
        self.payload = payload
        self.output_text = output_text

    def model_dump(self, **kwargs):
        del kwargs
        return self.payload


class _Responses:
    def __init__(self) -> None:
        self.requests = []
        self.results = [
            _Response(
                {
                    "id": "resp-1",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "lookup",
                            "arguments": '{"email":"ada@example.com"}',
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
                }
            ),
            _Response(
                {
                    "id": "resp-2",
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 15, "output_tokens": 4, "total_tokens": 19},
                },
                output_text="Ada Lovelace",
            ),
        ]

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.results.pop(0)


def test_openai_driver_translates_function_calls_and_outputs() -> None:
    responses = _Responses()
    driver = OpenAIResponsesDriver(client=SimpleNamespace(responses=responses))
    session = driver.open_session(
        task=EvalTask(id="lookup", prompt="Find Ada"),
        tools=[
            ToolDefinition(
                name="lookup",
                description="Find a customer.",
                input_schema={"type": "object", "properties": {"email": {"type": "string"}}},
            )
        ],
        instructions="Use tools.",
        config=AgentConfig(provider="openai", model="test-model"),
    )

    first = asyncio.run(session.respond([]))
    second = asyncio.run(
        session.respond([ToolOutput(call_id="call-1", output='{"name":"Ada Lovelace"}')])
    )

    assert first.tool_calls[0].arguments == {"email": "ada@example.com"}
    assert first.usage.total_tokens == 13
    assert second.text == "Ada Lovelace"
    assert responses.requests[0]["parallel_tool_calls"] is False
    assert any(
        item.get("type") == "function_call_output"
        for item in responses.requests[1]["input"]
        if isinstance(item, dict)
    )
