from pathlib import Path

import pytest

from mcp_doctor.cli import main
from mcp_doctor.diff import save_report
from mcp_doctor.models import (
    AnalysisReport,
    CheckResult,
    Finding,
    ServerInspection,
    Severity,
    ToolDefinition,
)
from mcp_doctor.surface_compare import compare_saved_surfaces


def _report(
    tools: list[ToolDefinition], *, metrics: dict[str, int] | None = None
) -> AnalysisReport:
    return AnalysisReport(
        inspection=ServerInspection(target="synthetic", tools=tools),
        metrics=metrics or {"tool_count": len(tools)},
        checks=[
            CheckResult(
                code="test",
                title="Test",
                passed=False,
                summary="test",
                findings=[
                    Finding(
                        rule_id="test.finding",
                        severity=Severity.WARNING,
                        tools=["shared"],
                        message="test",
                    )
                ],
            )
        ],
        policy={"version": 1},
    )


def test_symmetric_swap_and_shared_field_changes(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    save_report(
        _report(
            [
                ToolDefinition(
                    name="shared",
                    description="a",
                    input_schema={"type": "object", "required": ["a", "b"]},
                ),
                ToolDefinition(name="only_a"),
            ]
        ),
        a,
    )
    save_report(
        _report(
            [
                ToolDefinition(
                    name="shared",
                    description="b",
                    input_schema={"required": ["b", "a"], "type": "object"},
                    meta={"volatile": 1},
                ),
                ToolDefinition(name="only_b"),
            ]
        ),
        b,
    )
    forward = compare_saved_surfaces(a, b)
    reverse = compare_saved_surfaces(b, a)
    assert forward.only_a_tools == reverse.only_b_tools == ["only_a"]
    assert forward.only_b_tools == reverse.only_a_tools == ["only_b"]
    assert forward.shared_tools == reverse.shared_tools == ["shared"]
    assert (
        {(item.tool, item.field) for item in forward.changed_fields}
        == {(item.tool, item.field) for item in reverse.changed_fields}
        == {("shared", "description"), ("shared", "input_schema")}
    )
    assert [item.field for item in forward.changed_meta] == ["meta"]
    assert forward.metrics[0].b_minus_a == -reverse.metrics[0].b_minus_a


def test_tool_listing_failure_does_not_claim_absence(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    report = _report([ToolDefinition(name="one")])
    save_report(
        report.model_copy(
            update={
                "inspection": report.inspection.model_copy(
                    update={"listing_errors": {"tools": "timeout"}}
                )
            }
        ),
        a,
    )
    save_report(_report([ToolDefinition(name="two")]), b)
    comparison = compare_saved_surfaces(a, b)
    assert not comparison.valid_for_lab
    assert not comparison.tool_presence_known
    assert comparison.only_a_tools == comparison.only_b_tools == []
    assert "tool_listing_error" in {warning.code for warning in comparison.warnings}

    with pytest.raises(SystemExit) as exit_info:
        main(["compare", str(a), str(b)])
    assert exit_info.value.code == 2
