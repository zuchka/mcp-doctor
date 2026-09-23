from __future__ import annotations

import json
import re
from typing import Any

from mcp_doctor.lab.models import LabError, LabReport


def _display(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _interval(value: list[float] | None) -> str:
    return f"[{value[0]:.3f}, {value[1]:.3f}]" if value else "N/A"


def _tokens(value: dict[str, Any]) -> str:
    return json.dumps(
        {key: item if item is not None else "N/A" for key, item in value.items()},
        sort_keys=True,
    )


def render_markdown(report: LabReport) -> str:
    lines = [
        f"# MCP Scope Lab: {report.study_id}",
        "",
        f"Plan SHA-256: `{report.plan_sha256}`",
        "",
        "## Configuration",
        "",
    ]
    for key, value in report.configuration.items():
        lines.append(f"- {key}: `{json.dumps(value, sort_keys=True)}`")
    lines += [
        "",
        "## Results by condition",
        "",
        "| Condition | Scheduled | Scored | Passed | Failed | Infra errors | Not run | "
        "Pass rate | Wilson 95% | All-scheduled | Median calls | Median steps | "
        "Median duration ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | "
        "---: | ---: | ---: | ---: |",
    ]
    for cell in report.cells:
        interval = cell["wilson_95"]
        lines.append(
            "| "
            + " | ".join(
                [
                    cell["condition_id"],
                    str(cell["scheduled"]),
                    str(cell["scored"]),
                    str(cell["passed"]),
                    str(cell["failed"]),
                    str(cell["infrastructure_errors"]),
                    str(cell["not_run"]),
                    _display(cell["pass_rate"]),
                    _interval(interval),
                    _display(cell["all_scheduled_pass_rate"]),
                    _display(cell["median_tool_calls"]),
                    _display(cell["median_steps"]),
                    _display(cell["median_duration_ms"]),
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## Results by task",
        "",
        "| Task | Condition | Scored / scheduled | Passed | Failed | Infra errors | "
        "Not run | Pass rate | Wilson 95% | All-scheduled | Median calls | Median steps | "
        "Median duration ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for task in report.tasks:
        interval = task["wilson_95"]
        lines.append(
            f"| {task['task_id']} | {task['condition_id']} | "
            f"{task['scored']} / {task['scheduled']} | {task['passed']} | "
            f"{task['failed']} | {task['infrastructure_errors']} | {task['not_run']} | "
            f"{_display(task['pass_rate'])} | {_interval(interval)} | "
            f"{_display(task['all_scheduled_pass_rate'])} | "
            f"{_display(task['median_tool_calls'])} | "
            f"{_display(task['median_steps'])} | {_display(task['median_duration_ms'])} |"
        )
    lines += [
        "",
        "## Prespecified paired contrasts",
        "",
        "| Contrast | Complete pairs | First-only pass | Second-only pass | "
        "Second − first | Paired bootstrap 95% |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for contrast in report.contrasts:
        interval = contrast["paired_bootstrap_95"]
        interval_text = _interval(interval)
        lines.append(
            f"| {contrast['name']} | {contrast['complete_pairs']} | "
            f"{contrast['first_only_pass']} | {contrast['second_only_pass']} | "
            f"{_display(contrast['second_minus_first'])} | {interval_text} |"
        )
    did = report.difference_in_differences
    lines += [
        "",
        f"Difference in differences: {_display(did['estimate'])}; "
        f"complete four-cell blocks: {did['complete_blocks']}.",
        "",
        "## Secondary measures",
        "",
    ]
    for cell in report.cells:
        lines.append(
            f"- {cell['condition_id']}: "
            f"checks={json.dumps(cell['checks'], sort_keys=True)}; "
            f"docs calls={_display(cell['docs_calls'])}; "
            f"skills loaded={json.dumps(cell['skills_loaded'], sort_keys=True)}; "
            f"token distributions={_tokens(cell['tokens'])}"
        )
    lines += ["", "### By task and condition", ""]
    for task in report.tasks:
        lines.append(
            f"- {task['task_id']} / {task['condition_id']}: "
            f"checks={json.dumps(task['checks'], sort_keys=True)}; "
            f"docs calls={_display(task['docs_calls'])}; "
            f"skills loaded={json.dumps(task['skills_loaded'], sort_keys=True)}; "
            f"token distributions={_tokens(task['tokens'])}"
        )
    lines += ["", "## Exclusions", ""]
    lines += [
        f"- {item['task_id']} / repetition {item['repetition']} / "
        f"{item['condition_id']}: {item['status']}"
        for item in report.exclusions
    ] or ["None."]
    lines += ["", "## Metric definitions", ""]
    lines += [f"- {name}: {definition}" for name, definition in report.metric_definitions.items()]
    lines += ["", "## Limitations", ""]
    lines += [f"- {item}" for item in report.limitations]
    return "\n".join(lines) + "\n"


def public_export(report: LabReport) -> dict[str, Any]:
    """Allow-list public summary fields and reject accidental secrets or local paths."""
    payload = report.model_dump(mode="json")
    allowed = {
        "report_format_version",
        "study_id",
        "plan_sha256",
        "configuration",
        "cells",
        "tasks",
        "contrasts",
        "difference_in_differences",
        "exclusions",
        "metric_definitions",
        "limitations",
    }
    if set(payload) != allowed:
        raise LabError("Public export contains an unexpected report field.")
    serialized = json.dumps(payload)
    forbidden = re.compile(
        r"(?i)(access[_-]?token|api[_-]?key|password|secret|transcript|tool[_-]?arguments|tool[_-]?results|/Users/|/home/|postgres(?:ql)?://|https?://)"
    )
    if forbidden.search(serialized):
        raise LabError("Public export contains a path, credential, or transcript-shaped value.")
    return payload
