from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def resolve_target(raw_target: str) -> Any:
    parsed = urlparse(raw_target)
    if parsed.scheme in {"http", "https"}:
        return raw_target

    path = Path(raw_target).expanduser()
    if not path.exists():
        raise ValueError(f"Local server file does not exist: {path}")
    if path.suffix not in {".py", ".js"}:
        raise ValueError("Local targets must be .py or .js server files.")
    return path.resolve()


def resolve_config(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"MCP config file does not exist: {resolved}")
    try:
        payload = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read MCP config {resolved}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers"), dict):
        raise ValueError("MCP config must be a JSON object containing an 'mcpServers' object.")
    if not payload["mcpServers"]:
        raise ValueError("MCP config must contain at least one server.")
    return payload


def resolve_source(*, target: str | None, config: Path | None) -> tuple[Any, str]:
    if bool(target) == bool(config):
        raise ValueError("Provide exactly one TARGET or --config PATH.")
    if config:
        return resolve_config(config), str(config.expanduser().resolve())
    assert target is not None
    source = resolve_target(target)
    return source, str(source)
