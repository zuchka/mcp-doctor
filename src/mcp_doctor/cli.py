from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from mcp_doctor.analyzers import analyze
from mcp_doctor.diff import ReportFileError, compare_reports, load_report, save_report
from mcp_doctor.evals.compare import EvalComparisonError, compare_eval_runs
from mcp_doctor.evals.drivers import DriverError, OpenAIResponsesDriver
from mcp_doctor.evals.harness import EvalHarnessError, evaluate_suite, preflight_eval
from mcp_doctor.evals.io import EvalRunFileError, load_eval_run, save_eval_run
from mcp_doctor.evals.models import AgentConfig, Effect, HarnessConfig, RecordingMode
from mcp_doctor.evals.report import (
    render_eval_diff_json,
    render_eval_diff_text,
    render_eval_json,
    render_eval_text,
    render_preflight_json,
    render_preflight_text,
)
from mcp_doctor.evals.suite import (
    EvalConfigError,
    filter_suite,
    load_capability_map,
    load_eval_suite,
)
from mcp_doctor.inspector import InspectionError, inspect_server
from mcp_doctor.policy import AnalysisPolicy, PolicyError, load_policy
from mcp_doctor.report import render_diff_json, render_diff_text, render_json, render_text
from mcp_doctor.targets import resolve_config, resolve_source, resolve_target

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
    return parser


async def _run_inspect(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.policy) if args.policy else AnalysisPolicy()
        source, target_label = resolve_source(target=args.target, config=args.config)
        inspection = await inspect_server(source, timeout=args.timeout, target_label=target_label)
        report = analyze(inspection, policy)
        if args.save_report:
            save_report(report, args.save_report)
    except (ValueError, InspectionError, PolicyError, ReportFileError) as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2

    print(render_json(report) if args.json else render_text(report))
    return 0


def _run_diff(args: argparse.Namespace) -> int:
    try:
        baseline = load_report(args.baseline)
        current = load_report(args.current)
        diff = compare_reports(baseline, current)
    except ReportFileError as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2
    print(render_diff_json(diff) if args.json else render_diff_text(diff))
    return 1 if args.fail_on_new and diff.new_warning_count else 0


async def _run_eval(args: argparse.Namespace) -> int:
    try:
        suite = load_eval_suite(args.suite)
        suite = filter_suite(
            suite,
            task_ids=set(args.task) or None,
            tags=set(args.tag) or None,
        )
        capability_map = load_capability_map(args.capability_map)
        source, target_label = resolve_source(target=args.target, config=args.config)
        preflight = await preflight_eval(
            source,
            target_label=target_label,
            suite=suite,
            capability_map=capability_map,
            timeout=args.timeout,
        )
        if args.dry_run:
            print(
                render_preflight_json(preflight) if args.json else render_preflight_text(preflight)
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
        run = await evaluate_suite(
            source,
            target_label=target_label,
            suite=suite,
            capability_map=capability_map,
            driver=driver,
            agent_config=agent_config,
            harness_config=harness_config,
            timeout=args.timeout,
            preflight_result=preflight,
        )
        if args.save_run:
            save_eval_run(run, args.save_run)
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
        baseline = load_eval_run(args.baseline)
        current = load_eval_run(args.current)
        diff = compare_eval_runs(baseline, current)
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
    if args.command == "eval":
        raise SystemExit(asyncio.run(_run_eval(args)))
    if args.command == "eval-diff":
        raise SystemExit(_run_eval_diff(args))
    parser.error(f"Unknown command: {args.command}")
