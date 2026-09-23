"""Read-only Lab aggregation and prespecified paired contrasts."""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from mcp_doctor.lab.io import load_json
from mcp_doctor.lab.models import AttemptStatus, LabAttempt, LabPlan, LabReport, LedgerEvent
from mcp_doctor.lab.planner import load_plan
from mcp_doctor.lab.runner import load_ledger


def _distribution(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)

    def quantile(q: float) -> float:
        position = (len(ordered) - 1) * q
        lower = math.floor(position)
        upper = math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "n": len(ordered),
        "min": ordered[0],
        "q1": quantile(0.25),
        "median": statistics.median(ordered),
        "q3": quantile(0.75),
        "max": ordered[-1],
    }


def _wilson(passed: int, scored: int) -> list[float] | None:
    if scored == 0:
        return None
    z = 1.959963984540054
    proportion = passed / scored
    denominator = 1 + z * z / scored
    center = (proportion + z * z / (2 * scored)) / denominator
    half = (
        z
        * math.sqrt(proportion * (1 - proportion) / scored + z * z / (4 * scored * scored))
        / denominator
    )
    return [max(0.0, center - half), min(1.0, center + half)]


def _summary(scheduled: int, values: list[LabAttempt]) -> dict[str, Any]:
    scored = [item for item in values if item.passed is not None]
    passed = sum(item.passed is True for item in scored)
    checks: dict[str, dict[str, int]] = {}
    for item in scored:
        for name, success in item.checks.items():
            count = checks.setdefault(name, {"passed": 0, "observed": 0})
            count["observed"] += 1
            count["passed"] += int(success)
    tokens: dict[str, list[float]] = {
        name: [] for name in ("input", "cached_input", "output", "reasoning")
    }
    for item in scored:
        if item.usage_by_model:
            tokens["input"].append(
                sum(usage.input_tokens for usage in item.usage_by_model.values())
            )
            tokens["cached_input"].append(
                sum(usage.cached_input_tokens for usage in item.usage_by_model.values())
            )
            tokens["output"].append(
                sum(usage.output_tokens for usage in item.usage_by_model.values())
            )
            if all(usage.reasoning_tokens is not None for usage in item.usage_by_model.values()):
                tokens["reasoning"].append(
                    sum(usage.reasoning_tokens or 0 for usage in item.usage_by_model.values())
                )
    return {
        "scheduled": scheduled,
        "scored": len(scored),
        "passed": passed,
        "failed": len(scored) - passed,
        "infrastructure_errors": len(values) - len(scored),
        "not_run": scheduled - len(values),
        "pass_rate": passed / len(scored) if scored else None,
        "wilson_95": _wilson(passed, len(scored)),
        "all_scheduled_pass_rate": passed / scheduled if scheduled else None,
        "checks": dict(sorted(checks.items())),
        "median_tool_calls": statistics.median(
            [item.tool_call_count for item in scored if item.tool_call_count is not None]
        )
        if any(item.tool_call_count is not None for item in scored)
        else None,
        "median_steps": statistics.median(
            [item.step_count for item in scored if item.step_count is not None]
        )
        if any(item.step_count is not None for item in scored)
        else None,
        "median_duration_ms": statistics.median(
            [item.duration_ms for item in scored if item.duration_ms is not None]
        )
        if any(item.duration_ms is not None for item in scored)
        else None,
        "tokens": {name: _distribution(series) for name, series in tokens.items()},
        "docs_calls": _distribution(
            [item.docs_call_count for item in scored if item.docs_call_count is not None]
        ),
        "skills_loaded": dict(
            sorted(Counter(skill for item in scored for skill in item.skills_loaded).items())
        ),
    }


def _bootstrap(values: list[int], seed: int) -> list[float] | None:
    if not values:
        return None
    rng = random.Random(seed)
    samples = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(4000))
    return [samples[100], samples[3899]]


def _contrast(
    name: str,
    first: str,
    second: str,
    blocks: dict[tuple[str, int], dict[str, LabAttempt]],
    seed: int,
) -> dict[str, Any]:
    paired = [
        (cell[first].passed, cell[second].passed)
        for cell in blocks.values()
        if first in cell
        and second in cell
        and cell[first].passed is not None
        and cell[second].passed is not None
    ]
    differences = [int(b) - int(a) for a, b in paired]
    return {
        "name": name,
        "first": first,
        "second": second,
        "complete_pairs": len(paired),
        "first_only_pass": sum(a and not b for a, b in paired),
        "second_only_pass": sum(b and not a for a, b in paired),
        "both_pass": sum(a and b for a, b in paired),
        "both_fail": sum(not a and not b for a, b in paired),
        "second_minus_first": statistics.mean(differences) if differences else None,
        "paired_bootstrap_95": _bootstrap(differences, seed),
    }


def build_report(plan: LabPlan, events: list[LedgerEvent], plan_sha256: str) -> LabReport:
    latest: dict[tuple[str, int, str], LedgerEvent] = {}
    for event in events:
        latest[(event.task_id, event.repetition, event.condition_id)] = event
    attempts: dict[tuple[str, int, str], LabAttempt] = {}
    exclusions: list[dict[str, Any]] = []
    for item in plan.ordered_attempts:
        key = (item.task_id, item.repetition, item.condition_id)
        event = latest.get(key)
        if event and event.status != AttemptStatus.STARTED and event.attempt_path:
            attempt = load_json(Path(event.attempt_path), LabAttempt)
            attempts[key] = attempt
            if attempt.passed is None:
                exclusions.append(
                    {
                        "task_id": item.task_id,
                        "repetition": item.repetition,
                        "condition_id": item.condition_id,
                        "status": attempt.status.value,
                    }
                )
        else:
            exclusions.append(
                {
                    "task_id": item.task_id,
                    "repetition": item.repetition,
                    "condition_id": item.condition_id,
                    "status": "not_run" if not event else "interrupted",
                }
            )
    cells = []
    tasks = []
    for condition in plan.manifest.conditions:
        selected = [attempt for key, attempt in attempts.items() if key[2] == condition.id]
        cells.append(
            {
                "condition_id": condition.id,
                "surface": condition.surface,
                "skill_level": condition.skill_level,
                **_summary(len(plan.manifest.tasks) * plan.manifest.repetitions, selected),
            }
        )
        for task_id in plan.manifest.tasks:
            task_values = [
                attempt
                for key, attempt in attempts.items()
                if key[0] == task_id and key[2] == condition.id
            ]
            tasks.append(
                {
                    "condition_id": condition.id,
                    "task_id": task_id,
                    **_summary(plan.manifest.repetitions, task_values),
                }
            )
    by_cell = {(item.surface, item.skill_level): item.id for item in plan.manifest.conditions}
    blocks: dict[tuple[str, int], dict[str, LabAttempt]] = {}
    for (task_id, repetition, condition_id), attempt in attempts.items():
        blocks.setdefault((task_id, repetition), {})[condition_id] = attempt
    pairs = [
        ("scope_without_skills", by_cell["broad", "none"], by_cell["scoped", "none"]),
        ("scope_with_skills", by_cell["broad", "supabase"], by_cell["scoped", "supabase"]),
        ("skills_with_broad", by_cell["broad", "none"], by_cell["broad", "supabase"]),
        ("skills_with_scoped", by_cell["scoped", "none"], by_cell["scoped", "supabase"]),
    ]
    contrasts = [
        _contrast(name, first, second, blocks, plan.manifest.seed + index)
        for index, (name, first, second) in enumerate(pairs)
    ]
    did: list[int] = []
    for cell in blocks.values():
        if len(cell) == 4 and all(item.passed is not None for item in cell.values()):
            did.append(
                int(cell[by_cell["scoped", "supabase"]].passed)
                - int(cell[by_cell["broad", "supabase"]].passed)
                - int(cell[by_cell["scoped", "none"]].passed)
                + int(cell[by_cell["broad", "none"]].passed)
            )
    return LabReport(
        study_id=plan.manifest.id,
        plan_sha256=plan_sha256,
        configuration={
            "doctor_version": plan.manifest.sources.doctor_version,
            "doctor_commit": plan.manifest.sources.doctor_commit,
            "evals_commit": plan.manifest.sources.evals_commit,
            "mcp_server_version": plan.manifest.sources.mcp_server_version,
            "skill_commit": plan.manifest.sources.skill_commit
            or plan.manifest.sources.evals_commit,
            "model": plan.manifest.agent.model,
            "reasoning_effort": plan.manifest.agent.reasoning_effort,
            "harness": plan.manifest.agent.harness,
            "runtime": plan.manifest.runtime.model_dump(mode="json"),
            "manifest_sha256": plan.manifest_sha256,
            "contract_sha256": plan.contract_sha256,
            "static_report_sha256": plan.report_sha256,
            "surface_comparison_sha256": plan.surface_comparison_sha256,
            "surface_fingerprints": plan.surface_fingerprints,
            "comparability_warnings": list(plan.comparability_warnings),
            "experiment_config_sha256": {
                condition_id: settings.config_sha256
                for condition_id, settings in plan.experiment_settings.items()
            },
            "seed": plan.manifest.seed,
        },
        cells=cells,
        tasks=tasks,
        contrasts=contrasts,
        difference_in_differences={
            "complete_blocks": len(did),
            "estimate": statistics.mean(did) if did else None,
            "paired_bootstrap_95": _bootstrap(did, plan.manifest.seed + 10),
        },
        exclusions=exclusions,
        metric_definitions={
            "pass_rate": (
                "Scored passes divided by scored attempts; infrastructure errors excluded."
            ),
            "all_scheduled_pass_rate": (
                "Scored passes divided by all scheduled attempts; missing and errors "
                "count as unsuccessful in this sensitivity view."
            ),
            "wilson_95": "Wilson score interval for a binomial pass rate, 95% nominal coverage.",
            "median_metrics": (
                "Median over scored attempts with the metric present; missing is N/A."
            ),
            "token_use": (
                "Per-attempt sums across models. Cached input is included in input, and "
                "reasoning is included in output; neither is added again."
            ),
            "contrasts": (
                "Mean paired binary outcome difference (second minus first) within "
                "task/repetition blocks; bootstrap intervals resample whole blocks."
            ),
            "difference_in_differences": (
                "(Scoped minus broad with skills) minus (scoped minus broad without "
                "skills) on complete four-cell blocks."
            ),
        },
        limitations=[
            "Descriptive pilot; small samples do not establish statistical significance.",
            "Static catalogs do not by themselves prove runtime exposure; "
            "result provenance is checked separately.",
        ],
    )


def report_lab(
    plan_path: Path, *, markdown_path: Path | None = None, check_revisions: bool = True
) -> LabReport:
    plan, plan_hash = load_plan(plan_path, check_revisions=check_revisions)
    report = build_report(plan, load_ledger(plan, plan_hash), plan_hash)
    if markdown_path:
        from mcp_doctor.lab.report import render_markdown

        markdown_path = markdown_path.expanduser().resolve()
        from mcp_doctor.lab.io import write_bytes

        write_bytes(markdown_path, render_markdown(report).encode())
    return report
