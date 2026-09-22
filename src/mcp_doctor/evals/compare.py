from __future__ import annotations

from mcp_doctor.evals.models import EvalRun, EvalRunDiff, TaskEvalDelta


class EvalComparisonError(ValueError):
    """Raised when eval runs are not comparable."""


def _material_harness(run: EvalRun) -> dict[str, object]:
    return {
        "repetitions": run.harness.repetitions,
        "allowed_effects": run.harness.allowed_effects,
        "instructions": run.harness.instructions,
    }


def _ensure_compatible(baseline: EvalRun, current: EvalRun) -> None:
    problems = []
    if baseline.status != "completed" or current.status != "completed":
        problems.append("one or both runs are incomplete")
    if baseline.suite_fingerprint != current.suite_fingerprint:
        problems.append("suite fingerprint differs")
    if baseline.task_ids != current.task_ids:
        problems.append("selected task IDs differ")
    if baseline.agent != current.agent:
        problems.append("provider, model, or agent settings differ")
    if _material_harness(baseline) != _material_harness(current):
        problems.append("repetitions, allowed effects, or instructions differ")
    if problems:
        raise EvalComparisonError("Eval runs are not comparable: " + "; ".join(problems))


def compare_eval_runs(baseline: EvalRun, current: EvalRun) -> EvalRunDiff:
    _ensure_compatible(baseline, current)
    baseline_tasks = {item.task_id: item for item in baseline.task_aggregates}
    current_tasks = {item.task_id: item for item in current.task_aggregates}
    if set(baseline_tasks) != set(current_tasks):
        raise EvalComparisonError("Eval runs are not comparable: completed task sets differ")
    if set(baseline_tasks) != set(baseline.task_ids):
        raise EvalComparisonError("Eval runs are not comparable: one or more tasks are incomplete")

    task_deltas = []
    regressions = []
    improvements = []
    for task_id in baseline.task_ids:
        before = baseline_tasks[task_id]
        after = current_tasks[task_id]
        success_delta = after.success_rate - before.success_rate
        regression = (
            success_delta < 0
            or after.forbidden_calls > before.forbidden_calls
            or after.tool_errors > before.tool_errors
        )
        improvement = (
            success_delta > 0
            or after.irrelevant_calls < before.irrelevant_calls
            or after.forbidden_calls < before.forbidden_calls
            or after.tool_errors < before.tool_errors
            or after.median_calls < before.median_calls
        ) and not regression
        task_deltas.append(
            TaskEvalDelta(
                task_id=task_id,
                baseline_success_rate=before.success_rate,
                current_success_rate=after.success_rate,
                success_rate_delta=success_delta,
                median_calls_delta=after.median_calls - before.median_calls,
                irrelevant_calls_delta=after.irrelevant_calls - before.irrelevant_calls,
                forbidden_calls_delta=after.forbidden_calls - before.forbidden_calls,
                median_latency_ms_delta=after.median_latency_ms - before.median_latency_ms,
                average_input_tokens_delta=(
                    after.average_input_tokens - before.average_input_tokens
                ),
                regression=regression,
                improvement=improvement,
            )
        )
        if regression:
            reasons = []
            if success_delta < 0:
                reasons.append(f"success rate {success_delta:+.1%}")
            if after.forbidden_calls > before.forbidden_calls:
                reasons.append(f"forbidden calls +{after.forbidden_calls - before.forbidden_calls}")
            if after.tool_errors > before.tool_errors:
                reasons.append(f"tool errors +{after.tool_errors - before.tool_errors}")
            regressions.append(f"{task_id}: " + ", ".join(reasons))
        elif improvement:
            improvements.append(task_id)

    metric_deltas = {
        key: current.metrics[key] - baseline.metrics[key]
        for key in sorted(set(baseline.metrics) & set(current.metrics))
        if current.metrics[key] != baseline.metrics[key]
    }
    return EvalRunDiff(
        baseline_run_id=baseline.run_id,
        current_run_id=current.run_id,
        baseline_revision=baseline.revision,
        current_revision=current.revision,
        task_deltas=task_deltas,
        metric_deltas=metric_deltas,
        regressions=regressions,
        improvements=improvements,
    )
