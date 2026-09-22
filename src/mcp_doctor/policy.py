from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from mcp_doctor.models import CheckResult, Finding, Severity


class PolicyError(ValueError):
    """Raised when an analysis policy cannot be read or validated."""


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_count: int = Field(default=20, ge=0)
    total_definition_tokens: int = Field(default=8_000, ge=0)
    tool_definition_tokens: int = Field(default=2_000, ge=0)
    parameter_count: int = Field(default=10, ge=0)
    schema_depth: int = Field(default=4, ge=0)
    union_branches: int = Field(default=5, ge=0)
    description_min_chars: int = Field(default=30, ge=0)
    description_max_chars: int = Field(default=800, ge=0)
    semantic_overlap: float = Field(default=0.72, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_description_range(self) -> Thresholds:
        if self.description_max_chars < self.description_min_chars:
            raise ValueError("description_max_chars must be >= description_min_chars")
        return self


class Suppression(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: str = Field(min_length=1)
    tools: tuple[str, ...] = ()
    subject: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("rule", "reason")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @field_validator("tools")
    @classmethod
    def normalize_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        stripped = tuple(sorted({tool.strip() for tool in value if tool.strip()}))
        if len(stripped) != len(value):
            raise ValueError("tool names must be non-blank and unique")
        return stripped

    @field_validator("subject")
    @classmethod
    def normalize_subject(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def require_exact_target(self) -> Suppression:
        if not self.tools and self.subject is None:
            raise ValueError("a suppression must specify tools or a subject")
        return self


class AnalysisPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1, le=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    enabled_rules: tuple[str, ...] | None = None
    disabled_rules: tuple[str, ...] = ()
    suppressions: tuple[Suppression, ...] = ()

    @field_validator("enabled_rules", "disabled_rules")
    @classmethod
    def normalize_rules(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized = tuple(sorted({rule.strip() for rule in value if rule.strip()}))
        if len(normalized) != len(value):
            raise ValueError("rule names must be non-blank and unique")
        return normalized

    @model_validator(mode="after")
    def validate_rule_sets(self) -> AnalysisPolicy:
        if self.enabled_rules is not None:
            overlap = set(self.enabled_rules) & set(self.disabled_rules)
            if overlap:
                raise ValueError(
                    "rules cannot be both enabled and disabled: " + ", ".join(sorted(overlap))
                )
        suppression_keys = [(item.rule, item.tools, item.subject) for item in self.suppressions]
        if len(suppression_keys) != len(set(suppression_keys)):
            raise ValueError("duplicate suppressions are not allowed")
        return self

    def rule_enabled(self, rule_id: str) -> bool:
        def matches(configured: str) -> bool:
            return rule_id == configured or rule_id.startswith(configured + ".")

        if self.enabled_rules is not None and not any(matches(rule) for rule in self.enabled_rules):
            return False
        return not any(matches(rule) for rule in self.disabled_rules)


def load_policy(path: Path) -> AnalysisPolicy:
    resolved = path.expanduser().resolve()
    try:
        with resolved.open("rb") as stream:
            payload = tomllib.load(stream)
        if "version" not in payload:
            raise PolicyError(f"Invalid analysis policy {resolved}: missing required 'version'.")
        return AnalysisPolicy.model_validate(payload)
    except OSError as exc:
        raise PolicyError(f"Could not read analysis policy {resolved}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"Invalid TOML in analysis policy {resolved}: {exc}") from exc
    except ValidationError as exc:
        raise PolicyError(f"Invalid analysis policy {resolved}: {exc}") from exc


def _matching_suppression(finding: Finding, policy: AnalysisPolicy) -> Suppression | None:
    finding_tools = tuple(sorted(set(finding.tools)))
    for suppression in policy.suppressions:
        if suppression.rule != finding.rule_id:
            continue
        if suppression.tools and suppression.tools != finding_tools:
            continue
        if suppression.subject is not None and suppression.subject != finding.subject:
            continue
        return suppression
    return None


def apply_policy(checks: list[CheckResult], policy: AnalysisPolicy) -> list[CheckResult]:
    """Filter disabled rules and annotate exact suppressions without hiding them."""

    configured_checks = []
    for check in checks:
        findings = []
        for finding in check.findings:
            if not policy.rule_enabled(finding.rule_id):
                continue
            suppression = _matching_suppression(finding, policy)
            if suppression:
                finding = finding.model_copy(
                    update={
                        "suppressed": True,
                        "suppression_reason": suppression.reason,
                    }
                )
            findings.append(finding)
        findings.sort(
            key=lambda item: (item.rule_id, tuple(sorted(item.tools)), item.subject or "")
        )
        passed = not any(
            item.severity == Severity.WARNING and not item.suppressed for item in findings
        )
        configured_checks.append(check.model_copy(update={"findings": findings, "passed": passed}))
    return configured_checks
