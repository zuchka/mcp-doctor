from __future__ import annotations

import json
import os
from time import perf_counter
from typing import Any

from mcp_doctor.evals.drivers.base import DriverError
from mcp_doctor.evals.models import (
    AgentConfig,
    AgentTurn,
    EvalTask,
    ModelUsage,
    ToolOutput,
    ToolRequest,
)
from mcp_doctor.models import ToolDefinition


def _plain(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return value
    return {}


def _usage(value: Any) -> ModelUsage:
    payload = _plain(value)
    input_details = payload.get("input_tokens_details") or {}
    output_details = payload.get("output_tokens_details") or {}
    input_tokens = int(payload.get("input_tokens") or 0)
    output_tokens = int(payload.get("output_tokens") or 0)
    return ModelUsage(
        input_tokens=input_tokens,
        cached_input_tokens=int(input_details.get("cached_tokens") or 0),
        output_tokens=output_tokens,
        reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
        total_tokens=int(payload.get("total_tokens") or input_tokens + output_tokens),
    )


class _OpenAIResponsesSession:
    def __init__(
        self,
        *,
        client: Any,
        task: EvalTask,
        tools: list[ToolDefinition],
        instructions: str,
        config: AgentConfig,
    ) -> None:
        self._client = client
        self._instructions = instructions
        self._config = config
        self._input: list[Any] = [{"role": "user", "content": task.prompt}]
        self._tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
                "strict": False,
            }
            for tool in tools
        ]

    async def respond(self, tool_outputs: list[ToolOutput]) -> AgentTurn:
        self._input.extend(
            {
                "type": "function_call_output",
                "call_id": output.call_id,
                "output": output.output,
            }
            for output in tool_outputs
        )
        request: dict[str, Any] = {
            "model": self._config.model,
            "instructions": self._instructions,
            "input": self._input,
            "tools": self._tools,
            "parallel_tool_calls": False,
            "store": False,
        }
        if self._config.reasoning_effort:
            request["reasoning"] = {"effort": self._config.reasoning_effort}
        if self._config.max_output_tokens:
            request["max_output_tokens"] = self._config.max_output_tokens
        if self._config.temperature is not None:
            request["temperature"] = self._config.temperature

        started = perf_counter()
        try:
            response = await self._client.responses.create(**request)
        except Exception as exc:
            raise DriverError(f"OpenAI Responses request failed: {exc}") from exc
        latency_ms = (perf_counter() - started) * 1000

        response_payload = _plain(response)
        output_items = response_payload.get("output") or []
        self._input.extend(getattr(response, "output", None) or output_items)
        tool_calls = []
        for item in output_items:
            if item.get("type") != "function_call":
                continue
            raw_value = item.get("arguments") or "{}"
            raw_arguments = raw_value if isinstance(raw_value, str) else json.dumps(raw_value)
            arguments = None
            parse_error = None
            try:
                parsed = json.loads(raw_arguments)
                if not isinstance(parsed, dict):
                    raise ValueError("arguments must decode to an object")
                arguments = parsed
            except (json.JSONDecodeError, ValueError) as exc:
                parse_error = str(exc)
            tool_calls.append(
                ToolRequest(
                    call_id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=arguments,
                    raw_arguments=raw_arguments,
                    parse_error=parse_error,
                )
            )

        return AgentTurn(
            response_id=response_payload.get("id"),
            model=response_payload.get("model"),
            status=str(response_payload.get("status") or "completed"),
            text=str(response_payload.get("output_text") or getattr(response, "output_text", "")),
            tool_calls=tool_calls,
            usage=_usage(response_payload.get("usage")),
            latency_ms=latency_ms,
        )


class OpenAIResponsesDriver:
    """OpenAI Responses API driver using MCP tools as custom functions."""

    def __init__(self, *, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
            return
        if not os.getenv("OPENAI_API_KEY"):
            raise DriverError("OPENAI_API_KEY is required for the OpenAI eval provider.")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise DriverError(
                "OpenAI eval support is not installed. Run: uv sync --extra eval"
            ) from exc
        self._client = AsyncOpenAI()

    def open_session(
        self,
        *,
        task: EvalTask,
        tools: list[ToolDefinition],
        instructions: str,
        config: AgentConfig,
    ) -> _OpenAIResponsesSession:
        return _OpenAIResponsesSession(
            client=self._client,
            task=task,
            tools=tools,
            instructions=instructions,
            config=config,
        )
