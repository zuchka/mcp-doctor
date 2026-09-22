import json

import pytest

from mcp_doctor.cli import _resolve_config, _resolve_target


def test_resolves_http_and_local_targets(tmp_path) -> None:
    server = tmp_path / "server.py"
    server.write_text("# test server")

    assert _resolve_target("https://example.test/mcp") == "https://example.test/mcp"
    assert _resolve_target(str(server)) == server.resolve()


def test_resolves_mcp_config(tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {"command": "uv", "args": ["run", "server.py"]}
                }
            }
        )
    )

    assert _resolve_config(config)["mcpServers"]["demo"]["command"] == "uv"


def test_rejects_non_mcp_config(tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text("{}")

    with pytest.raises(ValueError, match="mcpServers"):
        _resolve_config(config)
