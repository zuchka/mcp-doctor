from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, computed_field

from mcp_doctor.models import AnalysisReport, Finding, Severity


class ReportFileError(ValueError):
    """Raised when a saved report cannot be read, validated, or written."""


class MetricDelta(BaseModel):
    name: str
    before: int | float
    after: int | float
    delta: int | float


class ReportDiff(BaseModel):
    baseline_target: str
    current_target: str
    baseline_analysis_version: str
    current_analysis_version: str
    added_tools: list[str] = Field(default_factory=list)
    removed_tools: list[str] = Field(default_factory=list)
    new_findings: list[Finding] = Field(default_factory=list)
    resolved_findings: list[Finding] = Field(default_factory=list)
    unchanged_findings: list[Finding] = Field(default_factory=list)
    newly_suppressed: list[Finding] = Field(default_factory=list)
    newly_active: list[Finding] = Field(default_factory=list)
    metric_deltas: list[MetricDelta] = Field(default_factory=list)
    policy_changed: bool = False
    analysis_changed: bool = False

    @computed_field
    @property
    def new_warning_count(self) -> int:
        return sum(
            finding.severity == Severity.WARNING
            for finding in [*self.new_findings, *self.newly_active]
        )


def _all_findings(report: AnalysisReport) -> list[Finding]:
    return [finding for check in report.checks for finding in check.findings]


def compare_reports(baseline: AnalysisReport, current: AnalysisReport) -> ReportDiff:
    baseline_findings = {finding.fingerprint: finding for finding in _all_findings(baseline)}
    current_findings = {finding.fingerprint: finding for finding in _all_findings(current)}
    baseline_ids = set(baseline_findings)
    current_ids = set(current_findings)

    new_findings = [
        current_findings[key]
        for key in sorted(current_ids - baseline_ids)
        if not current_findings[key].suppressed
    ]
    new_suppressed_findings = [
        current_findings[key]
        for key in sorted(current_ids - baseline_ids)
        if current_findings[key].suppressed
    ]
    resolved_findings = [
        baseline_findings[key]
        for key in sorted(baseline_ids - current_ids)
        if not baseline_findings[key].suppressed
    ]
    unchanged_findings = []
    newly_suppressed = new_suppressed_findings
    newly_active = []
    for key in sorted(baseline_ids & current_ids):
        before = baseline_findings[key]
        after = current_findings[key]
        if not before.suppressed and after.suppressed:
            newly_suppressed.append(after)
        elif before.suppressed and not after.suppressed:
            newly_active.append(after)
        elif not after.suppressed:
            unchanged_findings.append(after)

    metric_deltas = []
    for name in sorted(set(baseline.metrics) & set(current.metrics)):
        before = baseline.metrics[name]
        after = current.metrics[name]
        if before != after:
            metric_deltas.append(
                MetricDelta(name=name, before=before, after=after, delta=after - before)
            )

    baseline_tools = {tool.name for tool in baseline.inspection.tools}
    current_tools = {tool.name for tool in current.inspection.tools}
    return ReportDiff(
        baseline_target=baseline.inspection.target,
        current_target=current.inspection.target,
        baseline_analysis_version=baseline.analysis_version,
        current_analysis_version=current.analysis_version,
        added_tools=sorted(current_tools - baseline_tools),
        removed_tools=sorted(baseline_tools - current_tools),
        new_findings=new_findings,
        resolved_findings=resolved_findings,
        unchanged_findings=unchanged_findings,
        newly_suppressed=newly_suppressed,
        newly_active=newly_active,
        metric_deltas=metric_deltas,
        policy_changed=baseline.policy != current.policy,
        analysis_changed=baseline.analysis_version != current.analysis_version,
    )


def save_report(report: AnalysisReport, path: Path) -> None:
    resolved = path.expanduser().resolve()
    payload = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    temporary_path: str | None = None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=resolved.parent, delete=False
        ) as stream:
            temporary_path = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, resolved)
    except OSError as exc:
        if temporary_path:
            with suppress(OSError):
                Path(temporary_path).unlink(missing_ok=True)
        raise ReportFileError(f"Could not write report {resolved}: {exc}") from exc


def load_report(path: Path) -> AnalysisReport:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text())
        if not isinstance(payload, dict) or "report_format_version" not in payload:
            raise ReportFileError(
                f"Unversioned report {resolved}; regenerate it with MCP Doctor V0.2."
            )
        report = AnalysisReport.model_validate(payload)
    except OSError as exc:
        raise ReportFileError(f"Could not read report {resolved}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReportFileError(f"Invalid JSON report {resolved}: {exc}") from exc
    except ValidationError as exc:
        raise ReportFileError(f"Invalid report {resolved}: {exc}") from exc
    if report.report_format_version != 1:
        raise ReportFileError(
            f"Unsupported report format version {report.report_format_version} in {resolved}."
        )
    return report
