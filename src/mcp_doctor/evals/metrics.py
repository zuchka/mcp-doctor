from __future__ import annotations

import json
import math
import statistics

from mcp_doctor.evals.models import ModelUsage, TaskAggregate, TaskAttempt, ToolCallStatus
from mcp_doctor.models import ToolDefinition


def add_usage(total: ModelUsage, item: ModelUsage) -> ModelUsage:
    return ModelUsage(
        input_tokens=total.input_tokens + item.input_tokens,
        cached_input_tokens=total.cached_input_tokens + item.cached_input_tokens,
        output_tokens=total.output_tokens + item.output_tokens,
        reasoning_tokens=total.reasoning_tokens + item.reasoning_tokens,
        total_tokens=total.total_tokens + item.total_tokens,
    )


def tool_definition_size(tools: list[ToolDefinition]) -> tuple[int, int]:
    payload = json.dumps(
        [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return len(payload), math.ceil(len(payload) / 4)


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def aggregate_tasks(attempts: list[TaskAttempt]) -> list[TaskAggregate]:
    task_ids = list(dict.fromkeys(attempt.task_id for attempt in attempts))
    aggregates = []
    for task_id in task_ids:
        selected = [attempt for attempt in attempts if attempt.task_id == task_id]
        calls = [len(attempt.tool_calls) for attempt in selected]
        latencies = [attempt.total_latency_ms for attempt in selected]
        aggregates.append(
            TaskAggregate(
                task_id=task_id,
                attempts=len(selected),
                successes=sum(attempt.success for attempt in selected),
                success_rate=sum(attempt.success for attempt in selected) / len(selected),
                total_calls=sum(calls),
                irrelevant_calls=sum(
                    not call.relevant for attempt in selected for call in attempt.tool_calls
                ),
                forbidden_calls=sum(
                    call.forbidden for attempt in selected for call in attempt.tool_calls
                ),
                tool_errors=sum(
                    call.status != ToolCallStatus.SUCCEEDED
                    for attempt in selected
                    for call in attempt.tool_calls
                ),
                median_calls=float(statistics.median(calls)),
                median_latency_ms=float(statistics.median(latencies)),
                p95_latency_ms=float(_p95(latencies)),
                average_input_tokens=sum(item.usage.input_tokens for item in selected)
                / len(selected),
                average_output_tokens=sum(item.usage.output_tokens for item in selected)
                / len(selected),
            )
        )
    return aggregates


def run_metrics(attempts: list[TaskAttempt]) -> dict[str, int | float]:
    tool_calls = [call for attempt in attempts for call in attempt.tool_calls]
    successes = sum(attempt.success for attempt in attempts)
    unique_tasks = len({attempt.task_id for attempt in attempts})
    return {
        "tasks": unique_tasks,
        "attempts": len(attempts),
        "successes": successes,
        "success_rate": successes / len(attempts) if attempts else 0.0,
        "tool_calls": len(tool_calls),
        "relevant_calls": sum(call.relevant for call in tool_calls),
        "irrelevant_calls": sum(not call.relevant for call in tool_calls),
        "forbidden_calls": sum(call.forbidden for call in tool_calls),
        "tool_errors": sum(call.status != ToolCallStatus.SUCCEEDED for call in tool_calls),
        "model_latency_ms": sum(attempt.model_latency_ms for attempt in attempts),
        "tool_latency_ms": sum(attempt.tool_latency_ms for attempt in attempts),
        "total_latency_ms": sum(attempt.total_latency_ms for attempt in attempts),
        "input_tokens": sum(attempt.usage.input_tokens for attempt in attempts),
        "cached_input_tokens": sum(attempt.usage.cached_input_tokens for attempt in attempts),
        "output_tokens": sum(attempt.usage.output_tokens for attempt in attempts),
        "reasoning_tokens": sum(attempt.usage.reasoning_tokens for attempt in attempts),
        "total_tokens": sum(attempt.usage.total_tokens for attempt in attempts),
    }
