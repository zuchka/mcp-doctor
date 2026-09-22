"""Application workflows shared by the CLI and MCP server adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_doctor.analyzers import analyze
from mcp_doctor.diff import ReportDiff, compare_reports, load_report, save_report
from mcp_doctor.evals.compare import compare_eval_runs
from mcp_doctor.evals.drivers.base import AgentDriver
from mcp_doctor.evals.harness import evaluate_suite, preflight_eval
from mcp_doctor.evals.io import load_eval_run, save_eval_run
from mcp_doctor.evals.models import (
    AgentConfig,
    CapabilityMap,
    EvalPreflight,
    EvalRun,
    EvalRunDiff,
    EvalSuite,
    HarnessConfig,
)
from mcp_doctor.evals.suite import filter_suite, load_capability_map, load_eval_suite
from mcp_doctor.inspector import inspect_server
from mcp_doctor.lab.analysis import report_lab
from mcp_doctor.lab.backend import LabBackend
from mcp_doctor.lab.io import write_json
from mcp_doctor.lab.models import LabPlan, LabReport, LedgerEvent
from mcp_doctor.lab.planner import plan_lab
from mcp_doctor.lab.runner import run_lab
from mcp_doctor.models import AnalysisReport
from mcp_doctor.policy import AnalysisPolicy, load_policy
from mcp_doctor.surface_compare import SurfaceComparison, compare_saved_surfaces
from mcp_doctor.targets import resolve_source


@dataclass(frozen=True)
class PreparedEval:
    source: Any
    target_label: str
    suite: EvalSuite
    capability_map: CapabilityMap
    preflight: EvalPreflight


async def inspect_workflow(
    *,
    target: str | None = None,
    config: Path | None = None,
    policy_path: Path | None = None,
    timeout: float = 30.0,
    save_path: Path | None = None,
) -> AnalysisReport:
    policy = load_policy(policy_path) if policy_path else AnalysisPolicy()
    source, target_label = resolve_source(target=target, config=config)
    inspection = await inspect_server(source, timeout=timeout, target_label=target_label)
    report = analyze(inspection, policy)
    if save_path:
        save_report(report, save_path)
    return report


def compare_inspections(baseline: Path, current: Path) -> ReportDiff:
    return compare_reports(load_report(baseline), load_report(current))


async def prepare_eval(
    *,
    target: str | None = None,
    config: Path | None = None,
    suite_path: Path,
    capability_map_path: Path,
    task_ids: set[str] | None = None,
    tags: set[str] | None = None,
    timeout: float = 30.0,
) -> PreparedEval:
    suite = filter_suite(load_eval_suite(suite_path), task_ids=task_ids, tags=tags)
    capability_map = load_capability_map(capability_map_path)
    source, target_label = resolve_source(target=target, config=config)
    preflight = await preflight_eval(
        source,
        target_label=target_label,
        suite=suite,
        capability_map=capability_map,
        timeout=timeout,
    )
    return PreparedEval(source, target_label, suite, capability_map, preflight)


async def run_prepared_eval(
    prepared: PreparedEval,
    *,
    driver: AgentDriver,
    agent_config: AgentConfig,
    harness_config: HarnessConfig,
    timeout: float = 30.0,
    save_path: Path | None = None,
) -> EvalRun:
    run = await evaluate_suite(
        prepared.source,
        target_label=prepared.target_label,
        suite=prepared.suite,
        capability_map=prepared.capability_map,
        driver=driver,
        agent_config=agent_config,
        harness_config=harness_config,
        timeout=timeout,
        preflight_result=prepared.preflight,
    )
    if save_path:
        save_eval_run(run, save_path)
    return run


def compare_evaluations(baseline: Path, current: Path) -> EvalRunDiff:
    return compare_eval_runs(load_eval_run(baseline), load_eval_run(current))


def compare_surface_workflow(
    report_a: Path,
    report_b: Path,
    *,
    label_a: str = "A",
    label_b: str = "B",
    save_path: Path | None = None,
) -> SurfaceComparison:
    comparison = compare_saved_surfaces(report_a, report_b, label_a=label_a, label_b=label_b)
    if save_path:
        write_json(save_path, comparison)
    return comparison


def plan_lab_workflow(
    manifest_path: Path, *, save_path: Path | None = None
) -> tuple[LabPlan, Path]:
    plan = plan_lab(manifest_path)
    destination = (save_path or Path(plan.output_dir) / "plan.json").expanduser().resolve()
    write_json(destination, plan, exclusive=True)
    return plan, destination


def run_lab_workflow(
    plan_path: Path,
    *,
    max_attempts: int,
    resume: bool = False,
    backend: LabBackend | None = None,
) -> list[LedgerEvent]:
    return run_lab(plan_path, max_attempts=max_attempts, resume=resume, backend=backend)


def report_lab_workflow(
    plan_path: Path,
    *,
    markdown_path: Path | None = None,
) -> LabReport:
    return report_lab(plan_path, markdown_path=markdown_path)
