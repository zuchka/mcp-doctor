import asyncio
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp_tasks import call_tool_task

from mcp_doctor.analyzers import analyze
from mcp_doctor.cli import main
from mcp_doctor.diff import load_report
from mcp_doctor.evals.drivers import ScriptedAgentDriver
from mcp_doctor.evals.io import load_eval_run
from mcp_doctor.evals.models import AgentTurn, ToolCallStatus, ToolRequest
from mcp_doctor.inspector import inspect_server
from mcp_doctor.server import create_server

ROOT = Path(__file__).parents[1]
TARGET = ROOT / "examples" / "eval_server.py"
SUITE = ROOT / "examples" / "evals" / "crm-suite.toml"
MAP = ROOT / "examples" / "evals" / "crm-v1.toml"


def test_server_tool_surface_passes_doctor_analysis() -> None:
    async def check() -> None:
        report = analyze(await inspect_server(create_server()))
        assert report.warning_count == 0
        assert report.inspection.server_version == "0.5.0"
        assert [tool.name for tool in report.inspection.tools] == [
            "diagnose_mcp_server",
            "compare_inspection_reports",
            "compare_mcp_surfaces",
            "preflight_mcp_eval",
            "run_mcp_eval",
            "compare_eval_runs",
            "preflight_mcp_lab",
            "run_mcp_lab",
            "report_mcp_lab",
        ]

    asyncio.run(check())


def test_mcp_diagnosis_and_preflight_return_compact_results(tmp_path) -> None:
    async def check() -> None:
        report_path = tmp_path / "diagnosis.json"
        async with Client(create_server()) as client:
            diagnosis = await client.call_tool(
                "diagnose_mcp_server",
                {"target": str(TARGET), "save_path": str(report_path)},
            )
            assert diagnosis.data["report_path"] == str(report_path)
            assert diagnosis.data["warning_count"] == load_report(report_path).warning_count
            assert "checks" not in diagnosis.data
            assert "inspection" not in diagnosis.data
            inspection_diff = await client.call_tool(
                "compare_inspection_reports",
                {"baseline_path": str(report_path), "current_path": str(report_path)},
            )
            assert inspection_diff.data["new_warning_count"] == 0
            assert inspection_diff.data["added_tools"] == []
            peer_comparison = await client.call_tool(
                "compare_mcp_surfaces",
                {
                    "report_a_path": str(report_path),
                    "report_b_path": str(report_path),
                },
            )
            assert peer_comparison.data["valid_for_lab"]
            assert peer_comparison.data["only_a_tools"] == []
            assert peer_comparison.data["only_b_tools"] == []

            preflight = await client.call_tool(
                "preflight_mcp_eval",
                {
                    "target": str(TARGET),
                    "suite_path": str(SUITE),
                    "capability_map_path": str(MAP),
                    "task_ids": ["find-customer-name"],
                },
            )
            assert preflight.data["task_ids"] == ["find-customer-name"]
            assert preflight.data["model_calls"] == 0
            assert preflight.data["target_tool_calls"] == 0
            assert "inspection" not in preflight.data

    asyncio.run(check())


def test_native_background_eval_saves_run_without_api_calls(tmp_path, monkeypatch) -> None:
    script = ScriptedAgentDriver(
        {
            "find-customer-name": [
                AgentTurn(
                    tool_calls=[
                        ToolRequest(
                            call_id="lookup-1",
                            name="lookup_customer",
                            arguments={"email": "ada@example.com"},
                        )
                    ]
                ),
                AgentTurn(text="Ada Lovelace owns the account."),
            ]
        }
    )
    monkeypatch.setattr("mcp_doctor.server.OpenAIResponsesDriver", lambda: script)

    async def check() -> None:
        run_path = tmp_path / "run.json"
        async with Client(create_server()) as client:
            task = await call_tool_task(
                client,
                "run_mcp_eval",
                {
                    "target": str(TARGET),
                    "suite_path": str(SUITE),
                    "capability_map_path": str(MAP),
                    "model": "scripted-test",
                    "save_path": str(run_path),
                    "options": {"task_ids": ["find-customer-name"]},
                },
            )
            assert task.task_id
            result = await task.result()
            assert result.data["metrics"]["successes"] == 1
            assert result.data["run_path"] == str(run_path)
            saved = load_eval_run(run_path)
            assert saved.attempts[0].success
            assert saved.attempts[0].tool_calls[0].arguments is None
            eval_diff = await client.call_tool(
                "compare_eval_runs",
                {"baseline_path": str(run_path), "current_path": str(run_path)},
            )
            assert eval_diff.data["regression_count"] == 0

    asyncio.run(check())


def test_server_rejects_relative_paths_before_running_target() -> None:
    async def check() -> None:
        async with Client(create_server()) as client:
            with pytest.raises(ToolError, match="absolute path"):
                await client.call_tool("diagnose_mcp_server", {"target": "examples/eval_server.py"})

    asyncio.run(check())


def test_mcp_eval_keeps_write_tools_blocked_by_default(tmp_path, monkeypatch) -> None:
    script = ScriptedAgentDriver(
        {
            "find-customer-name": [
                AgentTurn(
                    tool_calls=[
                        ToolRequest(
                            call_id="write-1",
                            name="add_customer_note",
                            arguments={"customer_id": "C-42", "note": "do not save"},
                        )
                    ]
                ),
                AgentTurn(text="Could not save the note."),
            ]
        }
    )
    monkeypatch.setattr("mcp_doctor.server.OpenAIResponsesDriver", lambda: script)

    async def check() -> None:
        run_path = tmp_path / "blocked-run.json"
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "run_mcp_eval",
                {
                    "target": str(TARGET),
                    "suite_path": str(SUITE),
                    "capability_map_path": str(MAP),
                    "model": "scripted-test",
                    "save_path": str(run_path),
                    "options": {"task_ids": ["find-customer-name"]},
                },
            )
            assert result.data["metrics"]["successes"] == 0
            call = load_eval_run(run_path).attempts[0].tool_calls[0]
            assert call.status == ToolCallStatus.BLOCKED
            assert call.arguments is None

    asyncio.run(check())


def test_serve_command_dispatches_stdio(monkeypatch) -> None:
    called = {}
    monkeypatch.setattr(
        "mcp_doctor.server.mcp.run",
        lambda **kwargs: called.update(kwargs),
    )
    main(["serve"])
    assert called == {"transport": "stdio", "show_banner": False}
