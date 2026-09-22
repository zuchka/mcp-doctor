from __future__ import annotations

from typing import Protocol

from mcp_doctor.evals.models import AgentConfig, AgentTurn, EvalTask, ToolOutput
from mcp_doctor.models import ToolDefinition


class DriverError(RuntimeError):
    """Raised when an agent provider cannot produce the next turn."""


class AgentSession(Protocol):
    async def respond(self, tool_outputs: list[ToolOutput]) -> AgentTurn:
        """Produce the next model turn, optionally consuming prior tool outputs."""


class AgentDriver(Protocol):
    def open_session(
        self,
        *,
        task: EvalTask,
        tools: list[ToolDefinition],
        instructions: str,
        config: AgentConfig,
    ) -> AgentSession:
        """Create an isolated session for one task attempt."""
