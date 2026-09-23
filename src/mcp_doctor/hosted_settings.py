"""Strict operator configuration for the public hosted MCP surface."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class HostedConfigurationError(ValueError):
    """Raised when the hosted server cannot start safely."""


class HostedSettings(BaseModel):
    """Fail-closed settings controlled by the hosted service operator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_target_urls: tuple[str, ...] = Field(min_length=1)
    default_timeout_seconds: float = Field(default=20.0, gt=0, le=30.0)
    max_timeout_seconds: float = Field(default=30.0, gt=0, le=60.0)
    max_concurrency: int = Field(default=8, ge=1, le=100)
    max_response_bytes: int = Field(default=2_097_152, ge=4_096, le=10_000_000)
    max_components_per_kind: int = Field(default=250, ge=1, le=10_000)
    max_component_bytes: int = Field(default=131_072, ge=1_024, le=10_000_000)
    max_total_catalog_bytes: int = Field(default=2_097_152, ge=4_096, le=50_000_000)
    max_schema_depth: int = Field(default=32, ge=1, le=128)
    max_public_string_chars: int = Field(default=500, ge=32, le=4_000)
    max_findings: int = Field(default=25, ge=1, le=100)
    max_tool_names: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def validate_timeouts(self) -> HostedSettings:
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("default_timeout_seconds must not exceed max_timeout_seconds")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> HostedSettings:
        """Load hosted configuration without accepting permissive fallbacks."""

        source = os.environ if environ is None else environ
        raw_targets = source.get("MCP_DOCTOR_ALLOWED_TARGET_URLS")
        if raw_targets is None:
            raise HostedConfigurationError(
                "MCP_DOCTOR_ALLOWED_TARGET_URLS is required and must be a JSON array."
            )
        try:
            targets = json.loads(raw_targets)
        except json.JSONDecodeError as exc:
            raise HostedConfigurationError(
                "MCP_DOCTOR_ALLOWED_TARGET_URLS must be a valid JSON array."
            ) from exc
        if not isinstance(targets, list) or not all(isinstance(item, str) for item in targets):
            raise HostedConfigurationError(
                "MCP_DOCTOR_ALLOWED_TARGET_URLS must be a JSON array of strings."
            )

        field_env = {
            "default_timeout_seconds": "MCP_DOCTOR_DEFAULT_TIMEOUT_SECONDS",
            "max_timeout_seconds": "MCP_DOCTOR_MAX_TIMEOUT_SECONDS",
            "max_concurrency": "MCP_DOCTOR_MAX_CONCURRENCY",
            "max_response_bytes": "MCP_DOCTOR_MAX_RESPONSE_BYTES",
            "max_components_per_kind": "MCP_DOCTOR_MAX_COMPONENTS_PER_KIND",
            "max_component_bytes": "MCP_DOCTOR_MAX_COMPONENT_BYTES",
            "max_total_catalog_bytes": "MCP_DOCTOR_MAX_TOTAL_CATALOG_BYTES",
            "max_schema_depth": "MCP_DOCTOR_MAX_SCHEMA_DEPTH",
            "max_public_string_chars": "MCP_DOCTOR_MAX_PUBLIC_STRING_CHARS",
            "max_findings": "MCP_DOCTOR_MAX_FINDINGS",
            "max_tool_names": "MCP_DOCTOR_MAX_TOOL_NAMES",
        }
        payload: dict[str, Any] = {"allowed_target_urls": targets}
        for field_name, env_name in field_env.items():
            if env_name in source:
                payload[field_name] = source[env_name]
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise HostedConfigurationError(f"Invalid hosted configuration: {exc}") from exc
