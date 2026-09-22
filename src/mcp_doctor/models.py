from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class ToolDefinition(BaseModel):
    """Transport-independent representation of an MCP tool definition."""

    name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None


class InventoryItem(BaseModel):
    """Small, common view of a resource, resource template, or prompt."""

    name: str
    identifier: str | None = None
    description: str | None = None


class ServerInspection(BaseModel):
    target: str
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None
    instructions: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    tools: list[ToolDefinition] = Field(default_factory=list)
    resources: list[InventoryItem] = Field(default_factory=list)
    resource_templates: list[InventoryItem] = Field(default_factory=list)
    prompts: list[InventoryItem] = Field(default_factory=list)
    listing_errors: dict[str, str] = Field(default_factory=dict)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    message: str
    tools: list[str] = Field(default_factory=list)
    subject: str | None = None
    suggestion: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    suppressed: bool = False
    suppression_reason: str | None = None

    @computed_field
    @property
    def fingerprint(self) -> str:
        """Stable identity for baseline matching; presentation details are excluded."""

        identity = {
            "rule_id": self.rule_id,
            "subject": self.subject,
            "tools": sorted(set(self.tools)),
        }
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class CheckResult(BaseModel):
    code: str
    title: str
    passed: bool
    summary: str
    findings: list[Finding] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    report_format_version: int = 1
    analysis_version: str = "0.2"
    inspection: ServerInspection
    metrics: dict[str, int | float]
    checks: list[CheckResult]
    policy: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def warning_count(self) -> int:
        return sum(
            finding.severity == Severity.WARNING and not finding.suppressed
            for check in self.checks
            for finding in check.findings
        )

    @computed_field
    @property
    def info_count(self) -> int:
        return sum(
            finding.severity == Severity.INFO and not finding.suppressed
            for check in self.checks
            for finding in check.findings
        )

    @computed_field
    @property
    def suppressed_count(self) -> int:
        return sum(finding.suppressed for check in self.checks for finding in check.findings)
