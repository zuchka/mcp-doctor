import json
from pathlib import Path

import pytest

from mcp_doctor.cli import _resolve_config, _resolve_target, main
from mcp_doctor.diff import save_report
from mcp_doctor.models import (
    AnalysisReport,
    CheckResult,
    Finding,
    ServerInspection,
    Severity,
)

ROOT = Path(__file__).parents[1]


def test_resolves_http_and_local_targets(tmp_path) -> None:
    server = tmp_path / "server.py"
    server.write_text("# test server")

    assert _resolve_target("https://example.test/mcp") == "https://example.test/mcp"
    assert _resolve_target(str(server)) == server.resolve()


def test_resolves_mcp_config(tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"demo": {"command": "uv", "args": ["run", "server.py"]}}})
    )

    assert _resolve_config(config)["mcpServers"]["demo"]["command"] == "uv"


def test_rejects_non_mcp_config(tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text("{}")

    with pytest.raises(ValueError, match="mcpServers"):
        _resolve_config(config)


def test_diff_cli_can_fail_only_on_new_warnings(tmp_path, capsys) -> None:
    baseline_path = tmp_path / "baseline.json"
    current_path = tmp_path / "current.json"
    baseline = AnalysisReport(
        inspection=ServerInspection(target="test"),
        metrics={},
        checks=[],
    )
    current = AnalysisReport(
        inspection=ServerInspection(target="test"),
        metrics={},
        checks=[
            CheckResult(
                code="test",
                title="Test",
                passed=False,
                summary="one warning",
                findings=[
                    Finding(
                        rule_id="test.new",
                        severity=Severity.WARNING,
                        message="A new warning.",
                    )
                ],
            )
        ],
    )
    save_report(baseline, baseline_path)
    save_report(current, current_path)

    with pytest.raises(SystemExit) as exit_info:
        main(["diff", str(baseline_path), str(current_path), "--fail-on-new"])

    assert exit_info.value.code == 1
    assert "1 new active warning" in capsys.readouterr().out


def test_eval_cli_dry_run_validates_without_a_model(capfd) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "eval",
                str(ROOT / "examples" / "eval_server.py"),
                "--suite",
                str(ROOT / "examples" / "evals" / "crm-suite.toml"),
                "--capability-map",
                str(ROOT / "examples" / "evals" / "crm-v1.toml"),
                "--dry-run",
            ]
        )

    assert exit_info.value.code == 0
    assert "Preflight passed" in capfd.readouterr().out
