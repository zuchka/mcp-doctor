from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabError(ValueError):
    """Invalid study, upstream contract, artifact, or ledger."""


class Sources(StrictModel):
    evals_checkout: str
    evals_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    doctor_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    doctor_version: str = Field(min_length=1)
    mcp_server_version: str = Field(min_length=1)
    skill_checkout: str | None = None
    skill_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def skill_pin(self) -> Sources:
        if (self.skill_checkout is None) != (self.skill_commit is None):
            raise ValueError("External skill checkout and commit must be supplied together.")
        return self


class Agent(StrictModel):
    harness: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)


class Runtime(StrictModel):
    kind: Literal["platform-lite"]
    ephemeral: Literal[True] = True
    settings: dict[str, str | int | float | bool] = Field(default_factory=dict)


class Surface(StrictModel):
    features: tuple[str, ...]
    report: str

    @field_validator("features")
    @classmethod
    def unique_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Surface features must be nonempty and unique.")
        return value


class SkillLevel(StrictModel):
    skills: tuple[str, ...]

    @field_validator("skills")
    @classmethod
    def unique_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Skills must be unique.")
        return value


class Condition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    surface: str
    skill_level: str
    experiment: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class Limits(StrictModel):
    attempt_timeout_seconds: int = Field(gt=0)
    maximum_attempts: int = Field(gt=0)
    infrastructure_retries: int = Field(default=0, ge=0, le=5)


class LabManifest(StrictModel):
    version: Literal[1]
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    output_dir: str
    tasks: tuple[str, ...] = Field(min_length=1)
    repetitions: int = Field(ge=1)
    seed: int = Field(ge=0)
    sources: Sources
    agent: Agent
    runtime: Runtime
    surfaces: dict[str, Surface]
    skill_levels: dict[str, SkillLevel]
    conditions: tuple[Condition, ...]
    limits: Limits

    @field_validator("tasks")
    @classmethod
    def safe_task_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(r"[a-z][a-z0-9_-]*", item) is None for item in value):
            raise ValueError("Task IDs must be simple Evals IDs, not command options.")
        return value

    @model_validator(mode="after")
    def matrix(self) -> LabManifest:
        if len(self.tasks) != len(set(self.tasks)):
            raise ValueError("Task IDs must be unique.")
        if set(self.surfaces) != {"broad", "scoped"}:
            raise ValueError("The first Lab requires broad and scoped surfaces.")
        if set(self.skill_levels) != {"none", "supabase"}:
            raise ValueError("The first Lab requires none and supabase skill levels.")
        expected_broad = {"docs", "account", "database", "development", "debugging", "functions"}
        if set(self.surfaces["broad"].features) != expected_broad:
            raise ValueError("Broad features differ from the prespecified study.")
        if set(self.surfaces["scoped"].features) != {"docs", "database"}:
            raise ValueError("Scoped features differ from the prespecified study.")
        if self.skill_levels["none"].skills:
            raise ValueError("The none skill level must be empty.")
        if set(self.skill_levels["supabase"].skills) != {
            "supabase",
            "supabase-postgres-best-practices",
        }:
            raise ValueError("The supabase skill level must expose the two declared skills.")
        cells = [(item.surface, item.skill_level) for item in self.conditions]
        if len(cells) != 4 or set(cells) != {
            (surface, skill) for surface in self.surfaces for skill in self.skill_levels
        }:
            raise ValueError("Conditions must map one-to-one to all four cells.")
        if (
            len({item.id for item in self.conditions}) != 4
            or len({item.experiment for item in self.conditions}) != 4
        ):
            raise ValueError("Condition and experiment IDs must be unique.")
        logical = len(self.tasks) * self.repetitions * 4
        if logical * (1 + self.limits.infrastructure_retries) > self.limits.maximum_attempts:
            raise ValueError("Scheduled attempts plus allowed retries exceed maximum_attempts.")
        return self


class ContractTask(StrictModel):
    id: str
    interface: str
    runtime_supported: bool
    skills_override: bool = False


class ContractExperiment(StrictModel):
    id: str
    agent: Agent
    runtime: Runtime
    features: tuple[str, ...]
    skills: tuple[str, ...]
    mcp_server_version: str
    tool_names: tuple[str, ...]
    tool_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvalsContract(StrictModel):
    version: Literal[1]
    result_schema_version: Literal[1]
    run_index_base: Literal[1]
    evals_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    skill_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    tasks: tuple[ContractTask, ...]
    experiments: tuple[ContractExperiment, ...]


class PlannedAttempt(StrictModel):
    sequence: int
    task_id: str
    repetition: int
    condition_id: str
    experiment: str


class LabPlan(StrictModel):
    plan_format_version: Literal[1] = 1
    manifest_path: str
    manifest_sha256: str
    contract_path: str
    contract_sha256: str
    run_index_base: Literal[1]
    manifest: LabManifest
    selected_tasks: tuple[ContractTask, ...]
    experiment_settings: dict[str, ContractExperiment]
    report_sha256: dict[str, str]
    surface_fingerprints: dict[str, str]
    surface_comparison_sha256: str
    comparability_warnings: tuple[str, ...] = ()
    ordered_attempts: tuple[PlannedAttempt, ...]
    output_dir: str


class AttemptStatus(StrEnum):
    STARTED = "started"
    PASSED = "passed"
    FAILED = "failed"
    PROCESS_ERROR = "process_error"
    TIMEOUT = "timeout"
    MISSING_RESULT = "missing_result"
    CORRUPT_RESULT = "corrupt_result"
    PROVENANCE_MISMATCH = "provenance_mismatch"
    SCHEMA_ERROR = "schema_error"


class Usage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def cached_subset(self) -> Usage:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens exceed input tokens.")
        if self.cached_input_tokens != self.cache_read_input_tokens + self.cache_write_input_tokens:
            raise ValueError("Cached input does not equal cache reads plus writes.")
        return self


class LabAttempt(StrictModel):
    attempt_format_version: Literal[1] = 1
    study_id: str
    task_id: str
    repetition: int
    condition_id: str
    experiment: str
    sandbox_id: str | None = None
    status: AttemptStatus
    passed: bool | None
    checks: dict[str, bool] = Field(default_factory=dict)
    skills_available: tuple[str, ...] = ()
    skills_loaded: tuple[str, ...] = ()
    docs_call_count: int | None = None
    docs_call_metadata: dict[str, int | float | str | bool] = Field(default_factory=dict)
    step_count: int | None = None
    tool_call_count: int | None = None
    duration_ms: float | None = None
    usage_by_model: dict[str, Usage] = Field(default_factory=dict)
    raw_result_path: str | None = None
    raw_result_sha256: str | None = None
    error_category: str | None = None
    error_detail: str | None = None

    @model_validator(mode="after")
    def scored_status(self) -> LabAttempt:
        if self.status == AttemptStatus.PASSED and self.passed is not True:
            raise ValueError("Passed attempt requires passed=true.")
        if self.status == AttemptStatus.FAILED and self.passed is not False:
            raise ValueError("Failed attempt requires passed=false.")
        if (
            self.status not in (AttemptStatus.PASSED, AttemptStatus.FAILED)
            and self.passed is not None
        ):
            raise ValueError("Infrastructure attempt cannot have a task score.")
        if self.passed is not None and not self.sandbox_id:
            raise ValueError("Scored attempt requires an ephemeral sandbox ID.")
        return self


class LedgerEvent(StrictModel):
    ledger_format_version: Literal[1] = 1
    sequence: int
    plan_sha256: str
    study_id: str
    task_id: str
    repetition: int
    condition_id: str
    invocation: int
    started_at: str
    ended_at: str | None = None
    status: AttemptStatus
    command_argv: tuple[str, ...]
    result_path: str | None = None
    result_sha256: str | None = None
    attempt_path: str | None = None
    attempt_sha256: str | None = None
    error_category: str | None = None
    previous_sha256: str | None = None

    @model_validator(mode="after")
    def event_phase(self) -> LedgerEvent:
        if self.status == AttemptStatus.STARTED:
            if self.ended_at or self.attempt_path or self.attempt_sha256 or self.result_sha256:
                raise ValueError("Started event cannot contain completion data.")
        elif not self.ended_at or not self.attempt_path or not self.attempt_sha256:
            raise ValueError("Completed event requires end time and attempt artifact.")
        if self.result_sha256 and not self.result_path:
            raise ValueError("Result hash requires a result path.")
        return self


class LabReport(StrictModel):
    report_format_version: Literal[1] = 1
    study_id: str
    plan_sha256: str
    configuration: dict[str, Any]
    cells: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    contrasts: list[dict[str, Any]]
    difference_in_differences: dict[str, Any]
    exclusions: list[dict[str, Any]]
    metric_definitions: dict[str, str]
    limitations: list[str]
