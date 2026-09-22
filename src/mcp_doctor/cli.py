from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp_doctor.diff import ReportFileError
from mcp_doctor.evals.compare import EvalComparisonError
from mcp_doctor.evals.drivers import DriverError, OpenAIResponsesDriver
from mcp_doctor.evals.harness import EvalHarnessError
from mcp_doctor.evals.io import EvalRunFileError
from mcp_doctor.evals.models import AgentConfig, Effect, HarnessConfig, RecordingMode
from mcp_doctor.evals.report import (
    render_eval_diff_json,
    render_eval_diff_text,
    render_eval_json,
    render_eval_text,
    render_preflight_json,
    render_preflight_text,
)
from mcp_doctor.evals.suite import EvalConfigError
from mcp_doctor.inspector import InspectionError
from mcp_doctor.lab.io import write_json
from mcp_doctor.lab.models import LabError
from mcp_doctor.lab.planner import load_plan
from mcp_doctor.policy import PolicyError
from mcp_doctor.report import render_diff_json, render_diff_text, render_json, render_text
from mcp_doctor.surface_compare import ComparisonError, render_comparison
from mcp_doctor.targets import resolve_config, resolve_target
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

_resolve_config = resolve_config
_resolve_target = resolve_target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-doctor",
        description="Inspect an MCP server as an agent-facing interface.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="Connect, enumerate capabilities, and run deterministic checks."
    )
    inspect_parser.add_argument(
        "target",
        nargs="?",
        help="An http(s) MCP endpoint or local .py/.js server file.",
    )
    inspect_parser.add_argument(
        "--config",
        type=Path,
        help=(
            "A trusted MCP JSON config (supports command-based STDIO "
            "and custom transport settings)."
        ),
    )
    inspect_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Connection timeout in seconds."
    )
    inspect_parser.add_argument(
        "--policy", type=Path, help="A TOML file containing analysis thresholds and suppressions."
    )
    inspect_parser.add_argument(
        "--save-report", type=Path, help="Atomically save the versioned JSON report to this path."
    )
    inspect_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    diff_parser = subparsers.add_parser(
        "diff", help="Compare two saved reports without reconnecting to their servers."
    )
    diff_parser.add_argument("baseline", type=Path, help="Previously saved baseline report.")
    diff_parser.add_argument("current", type=Path, help="Current saved report.")
    diff_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    diff_parser.add_argument(
        "--fail-on-new",
        action="store_true",
        help="Exit 1 when the current report contains new active warnings.",
    )

    compare_parser = subparsers.add_parser(
        "compare", help="Compare two intentional peer surfaces from saved reports."
    )
    compare_parser.add_argument("report_a", type=Path)
    compare_parser.add_argument("report_b", type=Path)
    compare_parser.add_argument("--label-a", default="A")
    compare_parser.add_argument("--label-b", default="B")
    compare_parser.add_argument("--json", action="store_true")
    compare_parser.add_argument("--save-comparison", type=Path)

    lab_parser = subparsers.add_parser("lab", help="Plan, run, and report an external scope study.")
    lab_commands = lab_parser.add_subparsers(dest="lab_command", required=True)
    lab_plan = lab_commands.add_parser("plan", help="Validate and save a nonbillable study plan.")
    lab_plan.add_argument("manifest", type=Path)
    lab_plan.add_argument("--json", action="store_true")
    lab_plan.add_argument("--save-plan", type=Path)
    lab_run = lab_commands.add_parser("run", help="Execute a plan with billable model calls.")
    lab_run.add_argument("plan", type=Path)
    lab_run.add_argument("--resume", action="store_true")
    lab_run.add_argument("--max-attempts", type=int, required=True)
    lab_report = lab_commands.add_parser(
        "report", help="Aggregate a saved Lab ledger without execution."
    )
    lab_report.add_argument("plan", type=Path)
    lab_report.add_argument("--json", action="store_true")
    lab_report.add_argument("--markdown", type=Path)
    lab_report.add_argument(
        "--public-json",
        type=Path,
        help="Write a checked, publication-safe JSON summary.",
    )

    eval_parser = subparsers.add_parser(
        "eval", help="Run a representative task suite against an MCP server."
    )
    eval_parser.add_argument(
        "target", nargs="?", help="An http(s) MCP endpoint or local .py/.js server file."
    )
    eval_parser.add_argument(
        "--config",
        type=Path,
        help="A trusted MCP JSON config for command-based or custom transports.",
    )
    eval_parser.add_argument(
        "--suite", type=Path, required=True, help="Versioned eval suite TOML file."
    )
    eval_parser.add_argument(
        "--capability-map",
        type=Path,
        required=True,
        help="Server-revision capability map TOML file.",
    )
    eval_parser.add_argument(
        "--timeout", type=float, default=30.0, help="MCP connection timeout in seconds."
    )
    eval_parser.add_argument(
        "--provider", choices=("openai",), default="openai", help="Agent provider."
    )
    eval_parser.add_argument("--model", help="Provider model ID (required unless --dry-run).")
    eval_parser.add_argument("--reasoning-effort", help="Optional provider reasoning effort.")
    eval_parser.add_argument("--max-output-tokens", type=int)
    eval_parser.add_argument("--temperature", type=float)
    eval_parser.add_argument("--repetitions", type=int, default=1)
    eval_parser.add_argument(
        "--task", action="append", default=[], help="Run one task ID; repeat to select several."
    )
    eval_parser.add_argument(
        "--tag", action="append", default=[], help="Run tasks matching any selected tag."
    )
    eval_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate connectivity, inventory, suite, and mapping without a model or tool calls.",
    )
    eval_parser.add_argument("--fail-fast", action="store_true")
    eval_parser.add_argument(
        "--fail-on-task-failure",
        action="store_true",
        help="Exit 1 when any task attempt fails.",
    )
    eval_parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="Permit tools mapped with write effects.",
    )
    eval_parser.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Permit tools mapped with destructive effects.",
    )
    eval_parser.add_argument(
        "--record",
        choices=tuple(mode.value for mode in RecordingMode),
        default=RecordingMode.METADATA.value,
        help="Trace recording level (default: metadata).",
    )
    eval_parser.add_argument(
        "--redact-pattern",
        action="append",
        default=[],
        help="Regex replaced in redacted traces; repeat for multiple patterns.",
    )
    eval_parser.add_argument("--save-run", type=Path, help="Atomically save the eval run JSON.")
    eval_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    eval_diff_parser = subparsers.add_parser(
        "eval-diff", help="Compare two compatible saved eval runs."
    )
    eval_diff_parser.add_argument("baseline", type=Path)
    eval_diff_parser.add_argument("current", type=Path)
    eval_diff_parser.add_argument("--json", action="store_true")
    eval_diff_parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit 1 when task success, forbidden calls, or tool errors regress.",
    )
    subparsers.add_parser("serve", help="Serve MCP Doctor as a local STDIO MCP server.")
    return parser


async def _run_inspect(args: argparse.Namespace) -> int:
    try:
        report = await inspect_workflow(
            target=args.target,
            config=args.config,
            policy_path=args.policy,
            timeout=args.timeout,
            save_path=args.save_report,
        )
    except (ValueError, InspectionError, PolicyError, ReportFileError) as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2

    print(render_json(report) if args.json else render_text(report))
    return 0


def _run_diff(args: argparse.Namespace) -> int:
    try:
        diff = compare_inspections(args.baseline, args.current)
    except ReportFileError as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2
    print(render_diff_json(diff) if args.json else render_diff_text(diff))
    return 1 if args.fail_on_new and diff.new_warning_count else 0


def _run_compare(args: argparse.Namespace) -> int:
    try:
        comparison = compare_surface_workflow(
            args.report_a,
            args.report_b,
            label_a=args.label_a,
            label_b=args.label_b,
            save_path=args.save_comparison,
        )
    except (ValueError, ReportFileError, ComparisonError, LabError) as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(comparison.model_dump(mode="json"), indent=2, sort_keys=True)
        if args.json
        else render_comparison(comparison)
    )
    return 0 if comparison.tool_presence_known else 2


def _run_lab(args: argparse.Namespace) -> int:
    try:
        if args.lab_command == "plan":
            plan, path = plan_lab_workflow(args.manifest, save_path=args.save_plan)
            summary = {
                "study_id": plan.manifest.id,
                "model": plan.manifest.agent.model,
                "tasks": list(plan.manifest.tasks),
                "attempts": len(plan.ordered_attempts),
                "maximum_attempts": plan.manifest.limits.maximum_attempts,
                "plan_path": str(path),
                "model_calls": 0,
                "target_tool_calls": 0,
            }
            if args.json:
                print(json.dumps(summary, indent=2, sort_keys=True))
            else:
                print(
                    f"Lab plan saved to {path}: {summary['attempts']} attempts "
                    f"across {len(plan.manifest.tasks)} tasks and 4 conditions; "
                    f"model {summary['model']}."
                )
            return 0
        if args.lab_command == "run":
            plan, _ = load_plan(args.plan)
            print(
                f"Running model {plan.manifest.agent.model}: {len(plan.manifest.tasks)} tasks, "
                f"{len(plan.ordered_attempts)} scheduled attempts, "
                f"maximum {plan.manifest.limits.maximum_attempts} billable attempts."
            )
            events = run_lab_workflow(
                args.plan,
                max_attempts=args.max_attempts,
                resume=args.resume,
            )
            print(f"Lab ledger has {len(events)} events at {Path(plan.output_dir) / 'ledger'}.")
            return 0
        if args.lab_command == "report":
            report = report_lab_workflow(args.plan, markdown_path=args.markdown)
            if args.public_json:
                from mcp_doctor.lab.report import public_export

                write_json(args.public_json, public_export(report))
            if args.json:
                print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
            else:
                from mcp_doctor.lab.report import render_markdown

                print(render_markdown(report), end="")
            return 0
    except (ValueError, LabError, ReportFileError) as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2
    return 2


async def _run_eval(args: argparse.Namespace) -> int:
    try:
        prepared = await prepare_eval(
            target=args.target,
            config=args.config,
            suite_path=args.suite,
            capability_map_path=args.capability_map,
            task_ids=set(args.task) or None,
            tags=set(args.tag) or None,
            timeout=args.timeout,
        )
        if args.dry_run:
            print(
                render_preflight_json(prepared.preflight)
                if args.json
                else render_preflight_text(prepared.preflight)
            )
            return 0
        if not args.model:
            raise EvalConfigError("--model is required unless --dry-run is used.")

        allowed_effects = {Effect.READ}
        if args.allow_writes:
            allowed_effects.add(Effect.WRITE)
        if args.allow_destructive:
            allowed_effects.add(Effect.DESTRUCTIVE)
        agent_config = AgentConfig(
            provider=args.provider,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=args.max_output_tokens,
            temperature=args.temperature,
        )
        harness_config = HarnessConfig(
            repetitions=args.repetitions,
            allowed_effects=tuple(sorted(allowed_effects, key=lambda effect: effect.value)),
            recording=RecordingMode(args.record),
            redaction_patterns=tuple(args.redact_pattern),
            fail_fast=args.fail_fast,
        )
        driver = OpenAIResponsesDriver()
        run = await run_prepared_eval(
            prepared,
            driver=driver,
            agent_config=agent_config,
            harness_config=harness_config,
            timeout=args.timeout,
            save_path=args.save_run,
        )
    except (
        ValueError,
        DriverError,
        EvalConfigError,
        EvalHarnessError,
        EvalRunFileError,
    ) as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2

    print(render_eval_json(run) if args.json else render_eval_text(run))
    failed = run.metrics["successes"] != run.metrics["attempts"]
    return 1 if args.fail_on_task_failure and failed else 0


def _run_eval_diff(args: argparse.Namespace) -> int:
    try:
        diff = compare_evaluations(args.baseline, args.current)
    except (EvalRunFileError, EvalComparisonError) as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2
    print(render_eval_diff_json(diff) if args.json else render_eval_diff_text(diff))
    return 1 if args.fail_on_regression and diff.regression_count else 0


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        raise SystemExit(asyncio.run(_run_inspect(args)))
    if args.command == "diff":
        raise SystemExit(_run_diff(args))
    if args.command == "compare":
        raise SystemExit(_run_compare(args))
    if args.command == "lab":
        raise SystemExit(_run_lab(args))
    if args.command == "eval":
        raise SystemExit(asyncio.run(_run_eval(args)))
    if args.command == "eval-diff":
        raise SystemExit(_run_eval_diff(args))
    if args.command == "serve":
        from mcp_doctor.server import mcp

        mcp.run(transport="stdio", show_banner=False)
        return
    parser.error(f"Unknown command: {args.command}")
