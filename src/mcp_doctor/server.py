"""Local MCP interface for diagnosing another MCP server during development."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp_tasks import TasksExtension
from pydantic import BaseModel, ConfigDict, Field

from mcp_doctor import __version__
from mcp_doctor.diff import ReportFileError
from mcp_doctor.evals.compare import EvalComparisonError
from mcp_doctor.evals.drivers import DriverError, OpenAIResponsesDriver
from mcp_doctor.evals.harness import EvalHarnessError
from mcp_doctor.evals.io import EvalRunFileError
from mcp_doctor.evals.models import AgentConfig, Effect, HarnessConfig, RecordingMode
from mcp_doctor.evals.suite import EvalConfigError
from mcp_doctor.inspector import InspectionError
from mcp_doctor.lab.models import LabError
from mcp_doctor.lab.planner import load_plan
from mcp_doctor.lab.runner import load_ledger
from mcp_doctor.policy import PolicyError
from mcp_doctor.surface_compare import ComparisonError
from mcp_doctor.workflows import (
    compare_evaluations,
    compare_inspections,
    compare_surface_workflow,
    inspect_workflow,
    plan_lab_workflow,
    prepare_eval,
    report_lab_workflow,
    run_lab_workflow,
    run_prepared_eval,
)

_EXPECTED_ERRORS = (
    ValueError,
    DriverError,
    EvalComparisonError,
    EvalConfigError,
    EvalHarnessError,
    EvalRunFileError,
    InspectionError,
    PolicyError,
    ReportFileError,
    LabError,
    ComparisonError,
)
_MAX_SUMMARY_ITEMS = 25
_MAX_EVAL_ATTEMPTS = 25


class EvalOptions(BaseModel):
    """Optional limits and permissions for a billable representative-task eval."""

    model_config = ConfigDict(extra="forbid")

    task_ids: list[str] = Field(default_factory=list, description="Only these task IDs.")
    tags: list[str] = Field(default_factory=list, description="Only tasks matching these tags.")
    repetitions: int = Field(default=1, ge=1, le=_MAX_EVAL_ATTEMPTS)
    allow_writes: bool = False
    allow_destructive: bool = False
    recording: Literal["metadata", "redacted", "full"] = "metadata"
    redaction_patterns: list[str] = Field(default_factory=list)
    fail_fast: bool = False
    reasoning_effort: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


def _path(value: str | None, label: str) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path on the Doctor host: {value!r}")
    return path


def _target(value: str | None) -> str | None:
    if value is None:
        return None
    if urlparse(value).scheme in {"http", "https"}:
        return value
    _path(value, "target")
    return value


def _finding(item: Any) -> dict[str, Any]:
    return {
        "rule_id": item.rule_id,
        "severity": item.severity.value,
        "tools": item.tools,
        "subject": item.subject,
        "message": item.message,
        "suggestion": item.suggestion,
        "suppressed": item.suppressed,
        "suppression_reason": item.suppression_reason,
    }


def _limited(items: list[Any], convert: Any) -> tuple[list[Any], int]:
    return [convert(item) for item in items[:_MAX_SUMMARY_ITEMS]], max(
        0, len(items) - _MAX_SUMMARY_ITEMS
    )


def create_server() -> FastMCP:
    server = FastMCP(
        "MCP Doctor",
        version=__version__,
        instructions=(
            "Diagnose MCP surfaces and run pinned evaluations. Inspection, comparison, "
            "Lab planning, and reporting do not call target tools. Native eval and "
            "external Lab execution make billable model calls and may write sandbox state. "
            "All file paths are absolute paths on this machine."
        ),
        mask_error_details=True,
    )
    server.add_extension(TasksExtension(url="memory://"))

    @server.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def diagnose_mcp_server(
        target: str | None = None,
        config_path: str | None = None,
        policy_path: str | None = None,
        save_path: str | None = None,
        timeout: Annotated[float, Field(gt=0, le=120)] = 30.0,
    ) -> dict[str, Any]:
        """Use when a coding agent needs an inventory and deterministic interface diagnosis.

        Provide either a target URL or absolute local .py/.js path, or a trusted absolute MCP
        config path. This may launch a local server but never calls its tools. Do not use for
        representative task success; use preflight_mcp_eval or run_mcp_eval for that.
        """
        try:
            report = await inspect_workflow(
                target=_target(target),
                config=_path(config_path, "config_path"),
                policy_path=_path(policy_path, "policy_path"),
                timeout=timeout,
                save_path=_path(save_path, "save_path"),
            )
            findings = [item for check in report.checks for item in check.findings]
            findings.sort(
                key=lambda item: (
                    item.suppressed,
                    item.severity.value != "warning",
                    item.rule_id,
                    item.tools,
                )
            )
            selected, omitted = _limited(findings, _finding)
            tool_names, omitted_tools = _limited(
                [tool.name for tool in report.inspection.tools], lambda name: name
            )
            return {
                "target": report.inspection.target,
                "server_name": report.inspection.server_name,
                "server_version": report.inspection.server_version,
                "tool_names": tool_names,
                "omitted_tool_names": omitted_tools,
                "resources": len(report.inspection.resources),
                "resource_templates": len(report.inspection.resource_templates),
                "prompts": len(report.inspection.prompts),
                "listing_errors": report.inspection.listing_errors,
                "metrics": report.metrics,
                "warning_count": report.warning_count,
                "info_count": report.info_count,
                "suppressed_count": report.suppressed_count,
                "findings": selected,
                "omitted_findings": omitted,
                "report_path": str(Path(save_path).resolve()) if save_path else None,
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def compare_inspection_reports(baseline_path: str, current_path: str) -> dict[str, Any]:
        """Use when comparing two saved static inspection reports for interface regressions.

        Reads versioned JSON artifacts and reports new, resolved, and suppressed findings.
        Do not use for task success; use compare_eval_runs for saved eval artifacts.
        """
        try:
            diff = compare_inspections(
                _path(baseline_path, "baseline_path"),
                _path(current_path, "current_path"),
            )
            new, omitted_new = _limited(diff.new_findings, _finding)
            active, omitted_active = _limited(diff.newly_active, _finding)
            resolved, omitted_resolved = _limited(diff.resolved_findings, _finding)
            suppressed, omitted_suppressed = _limited(diff.newly_suppressed, _finding)
            added, omitted_added = _limited(diff.added_tools, lambda name: name)
            removed, omitted_removed = _limited(diff.removed_tools, lambda name: name)
            return {
                "baseline_target": diff.baseline_target,
                "current_target": diff.current_target,
                "new_warning_count": diff.new_warning_count,
                "new_findings": new,
                "omitted_new_findings": omitted_new,
                "newly_active": active,
                "omitted_newly_active": omitted_active,
                "resolved_findings": resolved,
                "omitted_resolved_findings": omitted_resolved,
                "newly_suppressed": suppressed,
                "omitted_newly_suppressed": omitted_suppressed,
                "unchanged_count": len(diff.unchanged_findings),
                "added_tools": added,
                "omitted_added_tools": omitted_added,
                "removed_tools": removed,
                "omitted_removed_tools": omitted_removed,
                "metric_deltas": [item.model_dump(mode="json") for item in diff.metric_deltas],
                "policy_changed": diff.policy_changed,
                "analysis_changed": diff.analysis_changed,
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations={"readOnlyHint": False, "openWorldHint": False})
    def compare_mcp_surfaces(
        report_a_path: str,
        report_b_path: str,
        label_a: str = "A",
        label_b: str = "B",
        save_path: str | None = None,
    ) -> dict[str, Any]:
        """Use when comparing two saved peer MCP surfaces symmetrically.

        Reads absolute local report paths. This is for intentional broad/scoped surfaces;
        compare_inspection_reports is for directional baseline regressions.
        """
        try:
            comparison = compare_surface_workflow(
                _path(report_a_path, "report_a_path"),
                _path(report_b_path, "report_b_path"),
                label_a=label_a,
                label_b=label_b,
                save_path=_path(save_path, "save_path"),
            )
            only_a, omitted_a = _limited(comparison.only_a_tools, lambda item: item)
            only_b, omitted_b = _limited(comparison.only_b_tools, lambda item: item)
            changes, omitted_changes = _limited(
                comparison.changed_fields,
                lambda item: item.model_dump(mode="json"),
            )
            metrics, omitted_metrics = _limited(
                comparison.metrics, lambda item: item.model_dump(mode="json")
            )
            warnings, omitted_warnings = _limited(
                comparison.warnings, lambda item: item.model_dump(mode="json")
            )
            return {
                "label_a": comparison.a.label,
                "label_b": comparison.b.label,
                "valid_for_lab": comparison.valid_for_lab,
                "only_a_tools": only_a,
                "omitted_only_a_tools": omitted_a,
                "only_b_tools": only_b,
                "omitted_only_b_tools": omitted_b,
                "shared_tool_count": len(comparison.shared_tools),
                "changed_fields": changes,
                "omitted_changed_fields": omitted_changes,
                "metrics": metrics,
                "omitted_metrics": omitted_metrics,
                "warnings": warnings,
                "omitted_warnings": omitted_warnings,
                "comparison_path": str(Path(save_path).resolve()) if save_path else None,
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def preflight_mcp_eval(
        suite_path: str,
        capability_map_path: str,
        target: str | None = None,
        config_path: str | None = None,
        task_ids: list[str] | None = None,
        tags: list[str] | None = None,
        timeout: Annotated[float, Field(gt=0, le=120)] = 30.0,
    ) -> dict[str, Any]:
        """Use when validating an eval suite and live tool mapping before a paid run.

        Connects to the target and inventories its interface without model or target tool calls.
        Do not use for measured task success; use run_mcp_eval for that.
        """
        try:
            prepared = await prepare_eval(
                target=_target(target),
                config=_path(config_path, "config_path"),
                suite_path=_path(suite_path, "suite_path"),
                capability_map_path=_path(capability_map_path, "capability_map_path"),
                task_ids=set(task_ids or ()) or None,
                tags=set(tags or ()) or None,
                timeout=timeout,
            )
            result = prepared.preflight
            selected_tasks, omitted_tasks = _limited(result.task_ids, lambda task_id: task_id)
            return {
                "target": result.target,
                "suite_name": result.suite_name,
                "revision": result.revision,
                "task_ids": selected_tasks,
                "omitted_task_ids": omitted_tasks,
                "tools": result.tools,
                "estimated_tool_definition_tokens": result.estimated_tool_definition_tokens,
                "suite_fingerprint": result.suite_fingerprint,
                "capability_map_fingerprint": result.capability_map_fingerprint,
                "interface_fingerprint": result.interface_fingerprint,
                "model_calls": 0,
                "target_tool_calls": 0,
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        task=True,
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    )
    async def run_mcp_eval(
        suite_path: str,
        capability_map_path: str,
        model: str,
        save_path: str,
        target: str | None = None,
        config_path: str | None = None,
        options: EvalOptions | None = None,
        timeout: Annotated[float, Field(gt=0, le=120)] = 30.0,
    ) -> dict[str, Any]:
        """Use when measuring whether an agent completes representative tasks on a server.

        This makes billable OpenAI Responses calls and invokes mapped target tools. Effects are
        read-only unless explicitly allowed in options. Saves the full versioned run at save_path.
        Do not use for inventory-only diagnosis; use diagnose_mcp_server or preflight_mcp_eval.
        """
        try:
            settings = options or EvalOptions()
            artifact = _path(save_path, "save_path")
            prepared = await prepare_eval(
                target=_target(target),
                config=_path(config_path, "config_path"),
                suite_path=_path(suite_path, "suite_path"),
                capability_map_path=_path(capability_map_path, "capability_map_path"),
                task_ids=set(settings.task_ids) or None,
                tags=set(settings.tags) or None,
                timeout=timeout,
            )
            attempts = len(prepared.suite.tasks) * settings.repetitions
            if attempts > _MAX_EVAL_ATTEMPTS:
                raise ValueError(
                    f"MCP eval is limited to {_MAX_EVAL_ATTEMPTS} attempts; "
                    f"selection requests {attempts}. Select fewer tasks or repetitions."
                )
            effects = {Effect.READ}
            if settings.allow_writes:
                effects.add(Effect.WRITE)
            if settings.allow_destructive:
                effects.add(Effect.DESTRUCTIVE)
            run = await run_prepared_eval(
                prepared,
                driver=OpenAIResponsesDriver(),
                agent_config=AgentConfig(
                    provider="openai",
                    model=model,
                    reasoning_effort=settings.reasoning_effort,
                    max_output_tokens=settings.max_output_tokens,
                    temperature=settings.temperature,
                ),
                harness_config=HarnessConfig(
                    repetitions=settings.repetitions,
                    allowed_effects=tuple(sorted(effects, key=lambda item: item.value)),
                    recording=RecordingMode(settings.recording),
                    redaction_patterns=tuple(settings.redaction_patterns),
                    fail_fast=settings.fail_fast,
                ),
                timeout=timeout,
                save_path=artifact,
            )
            failed = [
                {
                    "task_id": attempt.task_id,
                    "repetition": attempt.repetition,
                    "status": attempt.status.value,
                    "reason": attempt.failure_reason,
                }
                for attempt in run.attempts
                if not attempt.success
            ]
            return {
                "run_id": run.run_id,
                "status": run.status,
                "target": run.target,
                "suite_name": run.suite_name,
                "revision": run.revision,
                "model": run.agent.model,
                "task_ids": run.task_ids,
                "metrics": run.metrics,
                "failed_attempts": failed[:_MAX_SUMMARY_ITEMS],
                "omitted_failed_attempts": max(0, len(failed) - _MAX_SUMMARY_ITEMS),
                "run_path": str(artifact.resolve()),
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def compare_eval_runs(baseline_path: str, current_path: str) -> dict[str, Any]:
        """Use when comparing compatible saved representative-task eval runs.

        Reads versioned eval artifacts and reports success and call regressions by task.
        Do not use for static interface findings; use compare_inspection_reports.
        """
        try:
            diff = compare_evaluations(
                _path(baseline_path, "baseline_path"),
                _path(current_path, "current_path"),
            )
            deltas, omitted = _limited(diff.task_deltas, lambda item: item.model_dump(mode="json"))
            regressions, omitted_regressions = _limited(diff.regressions, lambda item: item)
            improvements, omitted_improvements = _limited(diff.improvements, lambda item: item)
            return {
                "baseline_run_id": diff.baseline_run_id,
                "current_run_id": diff.current_run_id,
                "baseline_revision": diff.baseline_revision,
                "current_revision": diff.current_revision,
                "regression_count": diff.regression_count,
                "regressions": regressions,
                "omitted_regressions": omitted_regressions,
                "improvements": improvements,
                "omitted_improvements": omitted_improvements,
                "task_deltas": deltas,
                "omitted_task_deltas": omitted,
                "metric_deltas": diff.metric_deltas,
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations={"readOnlyHint": False, "openWorldHint": False})
    def preflight_mcp_lab(manifest_path: str, save_plan_path: str | None = None) -> dict[str, Any]:
        """Use when validating a pinned Supabase Evals 2×2 study and saving a plan.

        Reads local static reports and a machine-readable Evals contract. No model or
        target-tool calls occur. All paths must be absolute on this Doctor host.
        """
        try:
            plan, path = plan_lab_workflow(
                _path(manifest_path, "manifest_path"),
                save_path=_path(save_plan_path, "save_plan_path"),
            )
            tasks, omitted = _limited(list(plan.manifest.tasks), lambda item: item)
            return {
                "study_id": plan.manifest.id,
                "model": plan.manifest.agent.model,
                "task_ids": tasks,
                "omitted_task_ids": omitted,
                "attempts": len(plan.ordered_attempts),
                "maximum_attempts": plan.manifest.limits.maximum_attempts,
                "plan_path": str(path),
                "model_calls": 0,
                "target_tool_calls": 0,
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        task=True,
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
    )
    async def run_mcp_lab(
        plan_path: str,
        max_attempts: Annotated[int, Field(gt=0)],
        resume: bool = False,
    ) -> dict[str, Any]:
        """Use when running an external Supabase Evals scorer from a pinned Lab plan.

        This native background task makes billable model calls and writes ephemeral sandbox
        state. Confirm the configured maximum with max_attempts. Doctor's run_mcp_eval
        instead uses a custom suite and deterministic Doctor graders.
        """
        try:
            path = _path(plan_path, "plan_path")
            plan, plan_hash = load_plan(path)
            await asyncio.to_thread(
                run_lab_workflow,
                path,
                max_attempts=max_attempts,
                resume=resume,
            )
            events = load_ledger(plan, plan_hash)
            statuses = [event.status.value for event in events if event.ended_at]
            return {
                "study_id": plan.manifest.id,
                "model": plan.manifest.agent.model,
                "scheduled_attempts": len(plan.ordered_attempts),
                "completed_attempts": len(statuses),
                "status_counts": {key: statuses.count(key) for key in sorted(set(statuses))},
                "ledger_path": str(Path(plan.output_dir) / "ledger"),
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations={"readOnlyHint": False, "openWorldHint": False})
    def report_mcp_lab(plan_path: str, markdown_path: str | None = None) -> dict[str, Any]:
        """Use when reporting Supabase Evals task-state scores from a saved Lab ledger.

        No model or target-tool calls occur. This reports external Supabase Evals scores,
        not Doctor's native representative-task grades.
        """
        try:
            report = report_lab_workflow(
                _path(plan_path, "plan_path"),
                markdown_path=_path(markdown_path, "markdown_path"),
            )
            cells, omitted_cells = _limited(report.cells, lambda item: item)
            tasks, omitted_tasks = _limited(report.tasks, lambda item: item)
            exclusions, omitted_exclusions = _limited(report.exclusions, lambda item: item)
            return {
                "study_id": report.study_id,
                "plan_sha256": report.plan_sha256,
                "cells": cells,
                "omitted_cells": omitted_cells,
                "tasks": tasks,
                "omitted_tasks": omitted_tasks,
                "contrasts": report.contrasts,
                "difference_in_differences": report.difference_in_differences,
                "exclusions": exclusions,
                "omitted_exclusions": omitted_exclusions,
                "markdown_path": str(Path(markdown_path).resolve()) if markdown_path else None,
                "model_calls": 0,
                "target_tool_calls": 0,
            }
        except _EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return server


mcp = create_server()
