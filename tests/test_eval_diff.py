from copy import deepcopy

import pytest

from mcp_doctor.evals.compare import EvalComparisonError, compare_eval_runs
from mcp_doctor.evals.io import load_eval_run, save_eval_run
from mcp_doctor.evals.models import (
    AgentConfig,
    CapabilityDefinition,
    CapabilityMap,
    EvalRun,
    EvalSuite,
    EvalTask,
    HarnessConfig,
    TaskAggregate,
)
from mcp_doctor.models import ServerInspection


def _run(*, success_rate: float, tool_errors: int = 0, revision: str = "v1") -> EvalRun:
    aggregate = TaskAggregate(
        task_id="task",
        attempts=2,
        successes=int(success_rate * 2),
        success_rate=success_rate,
        total_calls=2,
        irrelevant_calls=0,
        forbidden_calls=0,
        tool_errors=tool_errors,
        median_calls=1,
        median_latency_ms=10,
        p95_latency_ms=12,
        average_input_tokens=50,
        average_output_tokens=10,
    )
    suite = EvalSuite(
        version=1,
        name="suite",
        capabilities=(CapabilityDefinition(id="thing.read", description="Read a thing."),),
        tasks=(EvalTask(id="task", prompt="Read it", expected_capabilities=("thing.read",)),),
    )
    capability_map = CapabilityMap(version=1, suite="suite", revision=revision, tools={})
    return EvalRun(
        run_id=f"run-{revision}",
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        status="completed",
        target="test",
        revision=revision,
        suite_name="suite",
        suite=suite,
        suite_fingerprint="suite-fingerprint",
        capability_map=capability_map,
        capability_map_fingerprint=f"map-{revision}",
        interface_fingerprint=f"interface-{revision}",
        inspection=ServerInspection(target="test"),
        agent=AgentConfig(provider="scripted", model="test"),
        harness=HarnessConfig(repetitions=2),
        task_ids=["task"],
        tool_definition_bytes=100,
        estimated_tool_definition_tokens=25,
        attempts=[],
        task_aggregates=[aggregate],
        metrics={"success_rate": success_rate, "tool_errors": tool_errors},
    )


def test_detects_eval_regression_and_round_trips_run(tmp_path) -> None:
    baseline = _run(success_rate=1.0)
    current = _run(success_rate=0.5, tool_errors=1, revision="v2")
    path = tmp_path / "run.json"

    save_eval_run(current, path)
    loaded = load_eval_run(path)
    diff = compare_eval_runs(baseline, loaded)

    assert loaded == current
    assert diff.regression_count == 1
    assert diff.task_deltas[0].success_rate_delta == -0.5


def test_rejects_runs_with_different_agent_settings() -> None:
    baseline = _run(success_rate=1.0)
    current = deepcopy(baseline).model_copy(
        update={"agent": AgentConfig(provider="scripted", model="different")}
    )

    with pytest.raises(EvalComparisonError, match="agent settings"):
        compare_eval_runs(baseline, current)
