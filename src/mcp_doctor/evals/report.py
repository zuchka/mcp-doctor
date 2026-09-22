from __future__ import annotations

import json

from mcp_doctor.evals.models import EvalPreflight, EvalRun, EvalRunDiff


def render_eval_json(run: EvalRun) -> str:
    return json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True)


def render_preflight_json(preflight: EvalPreflight) -> str:
    return json.dumps(preflight.model_dump(mode="json"), indent=2, sort_keys=True)


def render_eval_diff_json(diff: EvalRunDiff) -> str:
    return json.dumps(diff.model_dump(mode="json"), indent=2, sort_keys=True)


def render_preflight_text(preflight: EvalPreflight) -> str:
    return "\n".join(
        [
            "MCP Doctor — Eval Preflight",
            "=" * 27,
            f"Target:    {preflight.target}",
            f"Suite:     {preflight.suite_name}",
            f"Revision:  {preflight.revision}",
            f"Tasks:     {len(preflight.task_ids)} ({', '.join(preflight.task_ids)})",
            f"Tools:     {preflight.tools}",
            (
                "Tool definitions: "
                f"{preflight.tool_definition_bytes:,} bytes / "
                f"~{preflight.estimated_tool_definition_tokens:,} tokens"
            ),
            "",
            "Preflight passed. No model or server tools were called.",
        ]
    )


def render_eval_text(run: EvalRun) -> str:
    lines = [
        "MCP Doctor — Eval Run",
        "=" * 21,
        f"Target:    {run.target}",
        f"Suite:     {run.suite_name}",
        f"Revision:  {run.revision}",
        f"Agent:     {run.agent.provider} / {run.agent.model}",
        f"Run ID:    {run.run_id}",
        "",
        "Task results",
        "------------",
    ]
    for aggregate in run.task_aggregates:
        lines.append(
            f"{'PASS' if aggregate.success_rate == 1 else 'FAIL':<4} "
            f"{aggregate.task_id}: {aggregate.successes}/{aggregate.attempts} passed; "
            f"{aggregate.total_calls} calls; {aggregate.irrelevant_calls} irrelevant; "
            f"{aggregate.tool_errors} errors"
        )
    lines.extend(
        [
            "",
            "Run metrics",
            "-----------",
            f"Success rate:      {run.metrics['success_rate']:.1%}",
            f"Tool calls:        {run.metrics['tool_calls']}",
            f"Irrelevant calls:  {run.metrics['irrelevant_calls']}",
            f"Forbidden calls:   {run.metrics['forbidden_calls']}",
            f"Tool errors:       {run.metrics['tool_errors']}",
            f"Input tokens:      {run.metrics['input_tokens']}",
            f"Output tokens:     {run.metrics['output_tokens']}",
            f"Total latency:     {run.metrics['total_latency_ms']:.1f} ms",
            (
                "Tool definitions:  "
                f"{run.tool_definition_bytes:,} bytes / "
                f"~{run.estimated_tool_definition_tokens:,} tokens"
            ),
            "",
            f"Result: {run.metrics['successes']}/{run.metrics['attempts']} attempts passed.",
        ]
    )
    return "\n".join(lines)


def render_eval_diff_text(diff: EvalRunDiff) -> str:
    lines = [
        "MCP Doctor — Eval Diff",
        "=" * 22,
        f"Baseline: {diff.baseline_revision} ({diff.baseline_run_id})",
        f"Current:  {diff.current_revision} ({diff.current_run_id})",
        "",
        "Task changes",
        "------------",
    ]
    for item in diff.task_deltas:
        marker = "REGRESS" if item.regression else "IMPROVE" if item.improvement else "SAME"
        lines.append(
            f"{marker:<7} {item.task_id}: success {item.success_rate_delta:+.1%}; "
            f"calls {item.median_calls_delta:+.1f}; irrelevant {item.irrelevant_calls_delta:+d}; "
            f"latency {item.median_latency_ms_delta:+.1f} ms"
        )
    if diff.regressions:
        lines.extend(["", "Regressions", "-----------"])
        lines.extend(f"- {item}" for item in diff.regressions)
    lines.extend(
        [
            "",
            f"Result: {diff.regression_count} regression"
            f"{'s' if diff.regression_count != 1 else ''}; "
            f"{len(diff.improvements)} improvement"
            f"{'s' if len(diff.improvements) != 1 else ''}.",
        ]
    )
    return "\n".join(lines)
