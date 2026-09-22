from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from mcp_doctor.connection import inspect_connected_client, open_mcp_client
from mcp_doctor.evals.drivers.base import AgentDriver
from mcp_doctor.evals.grading import grade_attempt
from mcp_doctor.evals.metrics import (
    add_usage,
    aggregate_tasks,
    run_metrics,
    tool_definition_size,
)
from mcp_doctor.evals.models import (
    AgentConfig,
    CapabilityMap,
    Effect,
    EvalPreflight,
    EvalRun,
    EvalSuite,
    EvalTask,
    HarnessConfig,
    ModelTurnTrace,
    ModelUsage,
    RecordingMode,
    TaskAttempt,
    TaskStatus,
    ToolCallStatus,
    ToolCallTrace,
    ToolOutput,
)
from mcp_doctor.evals.suite import (
    fingerprint,
    interface_fingerprint,
    validate_capability_map,
)
from mcp_doctor.models import ServerInspection
from mcp_doctor.normalize import model_to_dict


class EvalHarnessError(RuntimeError):
    """Raised when an eval run cannot be initialized or completed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _tool_output(call_id: str, *, result: Any = None, error: str | None = None) -> ToolOutput:
    payload = {}
    if error is not None:
        payload["error"] = error
    if result is not None:
        payload["result"] = result
    return ToolOutput(call_id=call_id, output=_json(payload))


def _trim_output(output: ToolOutput, maximum_bytes: int) -> tuple[ToolOutput, bool]:
    encoded = output.output.encode()
    if len(encoded) <= maximum_bytes:
        return output, False
    preview = encoded[: max(1, (maximum_bytes - 64) // 6)].decode(errors="ignore")
    truncated = _json({"preview": preview, "mcp_doctor_truncated": True})
    return ToolOutput(call_id=output.call_id, output=truncated), True


def _redact(value: Any, patterns: tuple[str, ...]) -> Any:
    if not patterns:
        return value
    if isinstance(value, str):
        for pattern in patterns:
            value = re.sub(pattern, "[REDACTED]", value)
        return value
    if isinstance(value, dict):
        return {key: _redact(item, patterns) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, patterns) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, patterns) for item in value)
    return value


def _record_attempt(attempt: TaskAttempt, config: HarnessConfig) -> TaskAttempt:
    if config.recording == RecordingMode.FULL:
        return attempt
    traces = []
    for call in attempt.tool_calls:
        if config.recording == RecordingMode.METADATA:
            traces.append(
                call.model_copy(update={"arguments": None, "raw_arguments": None, "result": None})
            )
        else:
            traces.append(
                call.model_copy(
                    update={
                        "arguments": _redact(call.arguments, config.redaction_patterns),
                        "raw_arguments": _redact(call.raw_arguments, config.redaction_patterns),
                        "result": _redact(call.result, config.redaction_patterns),
                    }
                )
            )
    return attempt.model_copy(update={"tool_calls": traces})


async def preflight_eval(
    source: Any,
    *,
    target_label: str,
    suite: EvalSuite,
    capability_map: CapabilityMap,
    timeout: float = 30.0,
) -> EvalPreflight:
    try:
        async with open_mcp_client(source, timeout=timeout) as client:
            inspection = await inspect_connected_client(client, target=target_label)
    except Exception as exc:
        raise EvalHarnessError(f"Could not connect to {target_label}: {exc}") from exc
    validate_capability_map(suite, capability_map, inspection)
    definition_bytes, definition_tokens = tool_definition_size(inspection.tools)
    return EvalPreflight(
        target=target_label,
        revision=capability_map.revision,
        suite_name=suite.name,
        suite_fingerprint=fingerprint(suite),
        capability_map_fingerprint=fingerprint(capability_map),
        interface_fingerprint=interface_fingerprint(inspection),
        task_ids=[task.id for task in suite.tasks],
        tools=len(inspection.tools),
        tool_definition_bytes=definition_bytes,
        estimated_tool_definition_tokens=definition_tokens,
        inspection=inspection,
    )


async def _attempt_body(
    source: Any,
    *,
    target_label: str,
    inspection: ServerInspection,
    task: EvalTask,
    repetition: int,
    capability_map: CapabilityMap,
    driver: AgentDriver,
    agent_config: AgentConfig,
    harness_config: HarnessConfig,
    connection_timeout: float,
    max_turns: int,
    max_tool_calls: int,
    tool_timeout: float,
    max_tool_result_bytes: int,
) -> TaskAttempt:
    started = perf_counter()
    model_turns = []
    tool_calls = []
    usage = ModelUsage()
    final_answer = ""
    failure_reason = None
    model_latency_ms = 0.0
    tool_latency_ms = 0.0
    allowed_effects = set(harness_config.allowed_effects)
    relevant_capabilities = set(task.expected_capabilities) | set(task.allowed_capabilities)
    forbidden_capabilities = set(task.forbidden_capabilities)

    async with open_mcp_client(source, timeout=connection_timeout) as client:
        live_inspection = await inspect_connected_client(client, target=target_label)
        if interface_fingerprint(live_inspection) != interface_fingerprint(inspection):
            raise EvalHarnessError("Server interface changed after eval preflight.")
        instructions = harness_config.instructions
        if live_inspection.instructions:
            instructions += "\n\nMCP server instructions:\n" + live_inspection.instructions
        session = driver.open_session(
            task=task,
            tools=live_inspection.tools,
            instructions=instructions,
            config=agent_config,
        )
        pending_outputs: list[ToolOutput] = []
        for turn_number in range(1, max_turns + 1):
            turn = await session.respond(pending_outputs)
            pending_outputs = []
            usage = add_usage(usage, turn.usage)
            model_latency_ms += turn.latency_ms
            model_turns.append(
                ModelTurnTrace(
                    turn=turn_number,
                    response_id=turn.response_id,
                    status=turn.status,
                    text=turn.text,
                    requested_tools=[request.name for request in turn.tool_calls],
                    usage=turn.usage,
                    latency_ms=turn.latency_ms,
                )
            )
            if turn.status != "completed":
                final_answer = turn.text
                failure_reason = f"Agent response ended with status {turn.status!r}."
                break
            if not turn.tool_calls:
                final_answer = turn.text
                break

            for request in turn.tool_calls:
                mapping = capability_map.tools.get(request.name)
                capabilities = list(mapping.capabilities) if mapping else []
                effect = mapping.effect if mapping else Effect.UNKNOWN
                relevant = bool(set(capabilities) & relevant_capabilities)
                forbidden = bool(set(capabilities) & forbidden_capabilities)
                trace = ToolCallTrace(
                    turn=turn_number,
                    call_id=request.call_id,
                    tool=request.name,
                    arguments=request.arguments,
                    raw_arguments=request.raw_arguments,
                    capabilities=capabilities,
                    effect=effect,
                    relevant=relevant,
                    forbidden=forbidden,
                    status=ToolCallStatus.INVALID,
                )
                if len(tool_calls) >= max_tool_calls:
                    trace.status = ToolCallStatus.BUDGET_EXCEEDED
                    trace.error = f"Tool-call budget of {max_tool_calls} exceeded."
                    tool_calls.append(trace)
                    pending_outputs.append(_tool_output(request.call_id, error=trace.error))
                    continue
                if request.parse_error or request.arguments is None:
                    trace.error = request.parse_error or "Tool arguments were not an object."
                    tool_calls.append(trace)
                    pending_outputs.append(_tool_output(request.call_id, error=trace.error))
                    continue
                if mapping is None:
                    trace.error = "Tool is not present in the capability map."
                    tool_calls.append(trace)
                    pending_outputs.append(_tool_output(request.call_id, error=trace.error))
                    continue
                if effect not in allowed_effects:
                    trace.status = ToolCallStatus.BLOCKED
                    trace.error = f"Effect {effect.value!r} is not allowed for this run."
                    tool_calls.append(trace)
                    pending_outputs.append(_tool_output(request.call_id, error=trace.error))
                    continue

                call_started = perf_counter()
                try:
                    raw_result = await client.call_tool_mcp(
                        request.name, request.arguments, timeout=tool_timeout
                    )
                    trace.latency_ms = (perf_counter() - call_started) * 1000
                    tool_latency_ms += trace.latency_ms
                    result = model_to_dict(raw_result)
                    trace.result = result
                    trace.result_bytes = len(_json(result).encode())
                    is_error = bool(result.get("isError") or result.get("is_error"))
                    trace.status = (
                        ToolCallStatus.MCP_ERROR if is_error else ToolCallStatus.SUCCEEDED
                    )
                    if is_error:
                        trace.error = "MCP tool returned an error result."
                    output, trace.result_truncated = _trim_output(
                        _tool_output(
                            request.call_id,
                            result=result,
                            error=trace.error if is_error else None,
                        ),
                        max_tool_result_bytes,
                    )
                    pending_outputs.append(output)
                except TimeoutError:
                    trace.latency_ms = (perf_counter() - call_started) * 1000
                    tool_latency_ms += trace.latency_ms
                    trace.status = ToolCallStatus.TIMEOUT
                    trace.error = f"Tool timed out after {tool_timeout:g} seconds."
                    pending_outputs.append(_tool_output(request.call_id, error=trace.error))
                except Exception as exc:
                    trace.latency_ms = (perf_counter() - call_started) * 1000
                    tool_latency_ms += trace.latency_ms
                    trace.status = ToolCallStatus.MCP_ERROR
                    trace.error = f"{type(exc).__name__}: {exc}"
                    pending_outputs.append(_tool_output(request.call_id, error=trace.error))
                tool_calls.append(trace)
        else:
            failure_reason = f"Agent exceeded the {max_turns}-turn limit."

    graders = grade_attempt(
        task,
        final_answer=final_answer,
        tool_calls=tool_calls,
        max_tool_calls=max_tool_calls,
    )
    success = failure_reason is None and all(result.passed for result in graders if result.required)
    if not success and failure_reason is None:
        failure_reason = "One or more required graders failed."
    return TaskAttempt(
        task_id=task.id,
        repetition=repetition,
        status=TaskStatus.PASSED if success else TaskStatus.FAILED,
        success=success,
        final_answer=final_answer,
        failure_reason=failure_reason,
        model_turns=model_turns,
        tool_calls=tool_calls,
        graders=graders,
        usage=usage,
        total_latency_ms=(perf_counter() - started) * 1000,
        model_latency_ms=model_latency_ms,
        tool_latency_ms=tool_latency_ms,
    )


async def _run_attempt(
    source: Any,
    *,
    target_label: str,
    inspection: ServerInspection,
    suite: EvalSuite,
    task: EvalTask,
    repetition: int,
    capability_map: CapabilityMap,
    driver: AgentDriver,
    agent_config: AgentConfig,
    harness_config: HarnessConfig,
    connection_timeout: float,
) -> TaskAttempt:
    max_turns = task.max_turns or suite.defaults.max_turns
    max_tool_calls = (
        task.max_tool_calls if task.max_tool_calls is not None else suite.defaults.max_tool_calls
    )
    task_timeout = task.task_timeout_seconds or suite.defaults.task_timeout_seconds
    tool_timeout = task.tool_timeout_seconds or suite.defaults.tool_timeout_seconds
    started = perf_counter()
    try:
        attempt = await asyncio.wait_for(
            _attempt_body(
                source,
                target_label=target_label,
                inspection=inspection,
                task=task,
                repetition=repetition,
                capability_map=capability_map,
                driver=driver,
                agent_config=agent_config,
                harness_config=harness_config,
                connection_timeout=connection_timeout,
                max_turns=max_turns,
                max_tool_calls=max_tool_calls,
                tool_timeout=tool_timeout,
                max_tool_result_bytes=suite.defaults.max_tool_result_bytes,
            ),
            timeout=task_timeout,
        )
    except TimeoutError:
        attempt = TaskAttempt(
            task_id=task.id,
            repetition=repetition,
            status=TaskStatus.TIMEOUT,
            success=False,
            failure_reason=f"Task timed out after {task_timeout:g} seconds.",
            total_latency_ms=(perf_counter() - started) * 1000,
        )
    except Exception as exc:
        attempt = TaskAttempt(
            task_id=task.id,
            repetition=repetition,
            status=TaskStatus.ERROR,
            success=False,
            failure_reason=f"{type(exc).__name__}: {exc}",
            total_latency_ms=(perf_counter() - started) * 1000,
        )
    return _record_attempt(attempt, harness_config)


async def evaluate_suite(
    source: Any,
    *,
    target_label: str,
    suite: EvalSuite,
    capability_map: CapabilityMap,
    driver: AgentDriver,
    agent_config: AgentConfig,
    harness_config: HarnessConfig,
    timeout: float = 30.0,
    preflight_result: EvalPreflight | None = None,
) -> EvalRun:
    started_at = _now()
    preflight = preflight_result or await preflight_eval(
        source,
        target_label=target_label,
        suite=suite,
        capability_map=capability_map,
        timeout=timeout,
    )
    attempts = []
    stopped_early = False
    for task in suite.tasks:
        for repetition in range(1, harness_config.repetitions + 1):
            attempt = await _run_attempt(
                source,
                target_label=target_label,
                inspection=preflight.inspection,
                suite=suite,
                task=task,
                repetition=repetition,
                capability_map=capability_map,
                driver=driver,
                agent_config=agent_config,
                harness_config=harness_config,
                connection_timeout=timeout,
            )
            attempts.append(attempt)
            if harness_config.fail_fast and not attempt.success:
                stopped_early = True
                break
        if stopped_early:
            break

    return EvalRun(
        run_id=str(uuid4()),
        started_at=started_at,
        completed_at=_now(),
        status="incomplete" if stopped_early else "completed",
        target=target_label,
        revision=capability_map.revision,
        suite_name=suite.name,
        suite=suite,
        suite_fingerprint=preflight.suite_fingerprint,
        capability_map=capability_map,
        capability_map_fingerprint=preflight.capability_map_fingerprint,
        interface_fingerprint=preflight.interface_fingerprint,
        inspection=preflight.inspection,
        agent=agent_config,
        harness=harness_config,
        task_ids=[task.id for task in suite.tasks],
        tool_definition_bytes=preflight.tool_definition_bytes,
        estimated_tool_definition_tokens=preflight.estimated_tool_definition_tokens,
        attempts=attempts,
        task_aggregates=aggregate_tasks(attempts),
        metrics=run_metrics(attempts),
    )
