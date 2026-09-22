from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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
    severity: Severity
    message: str
    tool: str | None = None
    suggestion: str | None = None


class CheckResult(BaseModel):
    code: str
    title: str
    passed: bool
    summary: str
    findings: list[Finding] = Field(default_factory=list)


class AnalysisReport(BaseModel):
    inspection: ServerInspection
    metrics: dict[str, int | float]
    checks: list[CheckResult]

    @property
    def warning_count(self) -> int:
        return sum(
            finding.severity == Severity.WARNING
            for check in self.checks
            for finding in check.findings
        )
