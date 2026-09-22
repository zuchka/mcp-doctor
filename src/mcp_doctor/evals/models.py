from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from mcp_doctor.models import ServerInspection


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class RecordingMode(StrEnum):
    METADATA = "metadata"
    REDACTED = "redacted"
    FULL = "full"


class TaskStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"


class ToolCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    MCP_ERROR = "mcp_error"
    INVALID = "invalid"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"


class CapabilityDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
    description: str = Field(min_length=1)


class EvalDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_turns: int = Field(default=6, ge=1)
    max_tool_calls: int = Field(default=4, ge=0)
    task_timeout_seconds: float = Field(default=60.0, gt=0)
    tool_timeout_seconds: float = Field(default=15.0, gt=0)
    max_tool_result_bytes: int = Field(default=50_000, ge=256)


class GraderDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["final_contains", "final_regex", "structured_result"]
    id: str | None = None
    required: bool = True
    values: tuple[str, ...] = ()
    pattern: str | None = None
    case_sensitive: bool = False
    tool: str | None = None
    path: str | None = None
    equals: Any = None

    @model_validator(mode="after")
    def validate_grader_fields(self) -> GraderDefinition:
        if self.type == "final_contains" and not self.values:
            raise ValueError("final_contains requires at least one value")
        if self.type == "final_regex" and not self.pattern:
            raise ValueError("final_regex requires pattern")
        if self.type == "structured_result" and (not self.tool or not self.path):
            raise ValueError("structured_result requires tool and path")
        return self


class EvalTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    prompt: str = Field(min_length=1)
    expected_capabilities: tuple[str, ...] = ()
    allowed_capabilities: tuple[str, ...] = ()
    forbidden_capabilities: tuple[str, ...] = ()
    expected_sequence: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    max_turns: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=0)
    task_timeout_seconds: float | None = Field(default=None, gt=0)
    tool_timeout_seconds: float | None = Field(default=None, gt=0)
    graders: tuple[GraderDefinition, ...] = ()

    @field_validator(
        "expected_capabilities",
        "allowed_capabilities",
        "forbidden_capabilities",
        "expected_sequence",
        "tags",
    )
    @classmethod
    def unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value

    @model_validator(mode="after")
    def validate_capability_sets(self) -> EvalTask:
        expected = set(self.expected_capabilities)
        allowed = set(self.allowed_capabilities)
        relevant = expected | allowed
        redundant = expected & allowed
        if redundant:
            raise ValueError(
                "capabilities cannot be both expected and allowed: " + ", ".join(sorted(redundant))
            )
        overlap = relevant & set(self.forbidden_capabilities)
        if overlap:
            raise ValueError(
                "capabilities cannot be both relevant and forbidden: " + ", ".join(sorted(overlap))
            )
        outside_sequence = set(self.expected_sequence) - relevant
        if outside_sequence:
            raise ValueError(
                "expected sequence must use expected or allowed capabilities: "
                + ", ".join(sorted(outside_sequence))
            )
        return self


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1, le=1)
    name: str = Field(min_length=1)
    defaults: EvalDefaults = Field(default_factory=EvalDefaults)
    capabilities: tuple[CapabilityDefinition, ...]
    tasks: tuple[EvalTask, ...]

    @model_validator(mode="after")
    def validate_suite(self) -> EvalSuite:
        capability_ids = [item.id for item in self.capabilities]
        task_ids = [item.id for item in self.tasks]
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("capability IDs must be unique")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique")
        known = set(capability_ids)
        for task in self.tasks:
            referenced = (
                set(task.expected_capabilities)
                | set(task.allowed_capabilities)
                | set(task.forbidden_capabilities)
                | set(task.expected_sequence)
            )
            unknown = referenced - known
            if unknown:
                raise ValueError(
                    f"task {task.id!r} references unknown capabilities: "
                    + ", ".join(sorted(unknown))
                )
        if not self.tasks:
            raise ValueError("an eval suite must contain at least one task")
        return self


class ToolCapabilityMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capabilities: tuple[str, ...]
    effect: Effect
    note: str | None = None

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must be unique")
        return value


class CapabilityMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=1, le=1)
    suite: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    tools: dict[str, ToolCapabilityMapping]


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class HarnessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repetitions: int = Field(default=1, ge=1)
    allowed_effects: tuple[Effect, ...] = (Effect.READ,)
    recording: RecordingMode = RecordingMode.METADATA
    redaction_patterns: tuple[str, ...] = ()
    fail_fast: bool = False
    instructions: str = (
        "Complete the user's task using the available tools when needed. "
        "Do not invent tool results. Stop when the task is answered."
    )

    @field_validator("redaction_patterns")
    @classmethod
    def valid_redaction_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import re

        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid redaction pattern {pattern!r}: {exc}") from exc
        return value

    @model_validator(mode="after")
    def validate_recording(self) -> HarnessConfig:
        if self.recording == RecordingMode.REDACTED and not self.redaction_patterns:
            raise ValueError("redacted recording requires at least one redaction pattern")
        return self


class EvalPreflight(BaseModel):
    target: str
    revision: str
    suite_name: str
    suite_fingerprint: str
    capability_map_fingerprint: str
    interface_fingerprint: str
    task_ids: list[str]
    tools: int
    tool_definition_bytes: int
    estimated_tool_definition_tokens: int
    inspection: ServerInspection


class ModelUsage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


class ToolRequest(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] | None = None
    raw_arguments: str | None = None
    parse_error: str | None = None


class ToolOutput(BaseModel):
    call_id: str
    output: str


class AgentTurn(BaseModel):
    response_id: str | None = None
    model: str | None = None
    status: str = "completed"
    text: str = ""
    tool_calls: list[ToolRequest] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    latency_ms: float = 0.0


class ModelTurnTrace(BaseModel):
    turn: int
    response_id: str | None = None
    status: str
    text: str
    requested_tools: list[str] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    latency_ms: float


class ToolCallTrace(BaseModel):
    turn: int
    call_id: str
    tool: str
    arguments: dict[str, Any] | None = None
    raw_arguments: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    effect: Effect = Effect.UNKNOWN
    relevant: bool = False
    forbidden: bool = False
    status: ToolCallStatus
    error: str | None = None
    latency_ms: float = 0.0
    result: Any = None
    result_bytes: int = 0
    result_truncated: bool = False


class GraderResult(BaseModel):
    grader_id: str
    passed: bool
    required: bool = True
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class TaskAttempt(BaseModel):
    task_id: str
    repetition: int
    status: TaskStatus
    success: bool
    final_answer: str = ""
    failure_reason: str | None = None
    model_turns: list[ModelTurnTrace] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    graders: list[GraderResult] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    total_latency_ms: float = 0.0
    model_latency_ms: float = 0.0
    tool_latency_ms: float = 0.0


class TaskAggregate(BaseModel):
    task_id: str
    attempts: int
    successes: int
    success_rate: float
    total_calls: int
    irrelevant_calls: int
    forbidden_calls: int
    tool_errors: int
    median_calls: float
    median_latency_ms: float
    p95_latency_ms: float
    average_input_tokens: float
    average_output_tokens: float


class EvalRun(BaseModel):
    eval_run_format_version: int = 1
    harness_version: str = "0.3"
    run_id: str
    started_at: str
    completed_at: str
    status: str
    target: str
    revision: str
    suite_name: str
    suite: EvalSuite
    suite_fingerprint: str
    capability_map: CapabilityMap
    capability_map_fingerprint: str
    interface_fingerprint: str
    inspection: ServerInspection
    agent: AgentConfig
    harness: HarnessConfig
    task_ids: list[str]
    tool_definition_bytes: int
    estimated_tool_definition_tokens: int
    attempts: list[TaskAttempt]
    task_aggregates: list[TaskAggregate]
    metrics: dict[str, int | float]


class TaskEvalDelta(BaseModel):
    task_id: str
    baseline_success_rate: float
    current_success_rate: float
    success_rate_delta: float
    median_calls_delta: float
    irrelevant_calls_delta: int
    forbidden_calls_delta: int
    median_latency_ms_delta: float
    average_input_tokens_delta: float
    regression: bool
    improvement: bool


class EvalRunDiff(BaseModel):
    baseline_run_id: str
    current_run_id: str
    baseline_revision: str
    current_revision: str
    task_deltas: list[TaskEvalDelta]
    metric_deltas: dict[str, int | float]
    regressions: list[str]
    improvements: list[str]
    compatible: bool = True

    @computed_field
    @property
    def regression_count(self) -> int:
        return len(self.regressions)
