from __future__ import annotations

from copy import deepcopy

from mcp_doctor.evals.drivers.base import DriverError
from mcp_doctor.evals.models import AgentConfig, AgentTurn, EvalTask, ToolOutput
from mcp_doctor.models import ToolDefinition


class _ScriptedSession:
    def __init__(self, turns: list[AgentTurn]) -> None:
        self._turns = turns
        self._index = 0

    async def respond(self, tool_outputs: list[ToolOutput]) -> AgentTurn:
        del tool_outputs
        if self._index >= len(self._turns):
            raise DriverError("Scripted agent ran out of turns.")
        turn = self._turns[self._index]
        self._index += 1
        return deepcopy(turn)


class ScriptedAgentDriver:
    """Deterministic driver for harness tests and offline integrations."""

    def __init__(self, scripts: dict[str, list[AgentTurn]]) -> None:
        self._scripts = scripts

    def open_session(
        self,
        *,
        task: EvalTask,
        tools: list[ToolDefinition],
        instructions: str,
        config: AgentConfig,
    ) -> _ScriptedSession:
        del tools, instructions, config
        try:
            turns = self._scripts[task.id]
        except KeyError as exc:
            raise DriverError(f"No scripted turns for task {task.id!r}.") from exc
        return _ScriptedSession(deepcopy(turns))
