import json

import pytest

from mcp_doctor.diff import ReportFileError, compare_reports, load_report, save_report
from mcp_doctor.models import (
    AnalysisReport,
    CheckResult,
    Finding,
    ServerInspection,
    Severity,
    ToolDefinition,
)


def _report(*findings: Finding, tools: tuple[str, ...] = ("first",)) -> AnalysisReport:
    return AnalysisReport(
        inspection=ServerInspection(
            target="test",
            tools=[ToolDefinition(name=name) for name in tools],
        ),
        metrics={"tool_count": len(tools)},
        checks=[
            CheckResult(
                code="test",
                title="Test",
                passed=not any(
                    finding.severity == Severity.WARNING and not finding.suppressed
                    for finding in findings
                ),
                summary="test",
                findings=list(findings),
            )
        ],
        policy={"version": 1},
    )


def _finding(name: str, *, suppressed: bool = False) -> Finding:
    return Finding(
        rule_id="test.warning",
        severity=Severity.WARNING,
        tools=[name],
        message=f"Finding for {name}.",
        suppressed=suppressed,
        suppression_reason="accepted" if suppressed else None,
    )


def test_diff_classifies_new_resolved_unchanged_and_suppressed() -> None:
    baseline = _report(
        _finding("resolved"),
        _finding("same"),
        _finding("activated", suppressed=True),
    )
    current = _report(
        _finding("same"),
        _finding("new"),
        _finding("suppressed", suppressed=True),
        _finding("activated"),
        tools=("first", "second"),
    )

    diff = compare_reports(baseline, current)

    assert [item.tools[0] for item in diff.new_findings] == ["new"]
    assert [item.tools[0] for item in diff.resolved_findings] == ["resolved"]
    assert [item.tools[0] for item in diff.unchanged_findings] == ["same"]
    assert [item.tools[0] for item in diff.newly_suppressed] == ["suppressed"]
    assert [item.tools[0] for item in diff.newly_active] == ["activated"]
    assert diff.added_tools == ["second"]
    assert diff.new_warning_count == 2


def test_saved_report_round_trip(tmp_path) -> None:
    path = tmp_path / "report.json"
    report = _report(_finding("first"))

    save_report(report, path)

    loaded = load_report(path)
    assert loaded.model_dump(mode="json") == report.model_dump(mode="json")


def test_rejects_unversioned_report(tmp_path) -> None:
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"inspection": {"target": "test"}, "metrics": {}, "checks": []}))

    with pytest.raises(ReportFileError, match="Unversioned report"):
        load_report(path)


def test_diff_discloses_analyzer_version_changes() -> None:
    baseline = _report()
    current = _report().model_copy(update={"analysis_version": "0.2.1"})

    diff = compare_reports(baseline, current)

    assert diff.analysis_changed
    assert diff.baseline_analysis_version == "0.2"
    assert diff.current_analysis_version == "0.2.1"
