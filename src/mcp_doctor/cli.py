from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp_doctor.analyzers import analyze
from mcp_doctor.inspector import InspectionError, inspect_server
from mcp_doctor.report import render_json, render_text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-doctor",
        description="Inspect an MCP server as an agent-facing interface.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="Connect, enumerate capabilities, and run deterministic checks."
    )
    inspect_parser.add_argument(
        "target",
        nargs="?",
        help="An http(s) MCP endpoint or local .py/.js server file.",
    )
    inspect_parser.add_argument(
        "--config",
        type=Path,
        help=(
            "A trusted MCP JSON config (supports command-based STDIO "
            "and custom transport settings)."
        ),
    )
    inspect_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Connection timeout in seconds."
    )
    inspect_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def _resolve_target(raw_target: str) -> Any:
    parsed = urlparse(raw_target)
    if parsed.scheme in {"http", "https"}:
        return raw_target

    path = Path(raw_target).expanduser()
    if not path.exists():
        raise ValueError(f"Local server file does not exist: {path}")
    if path.suffix not in {".py", ".js"}:
        raise ValueError("Local targets must be .py or .js server files.")
    return path.resolve()


def _resolve_config(path: Path) -> dict[str, Any]:
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


async def _run_inspect(args: argparse.Namespace) -> int:
    try:
        if bool(args.target) == bool(args.config):
            raise ValueError("Provide exactly one TARGET or --config PATH.")
        if args.config:
            source = _resolve_config(args.config)
            target_label = str(args.config.expanduser().resolve())
        else:
            source = _resolve_target(args.target)
            target_label = None
        inspection = await inspect_server(
            source, timeout=args.timeout, target_label=target_label
        )
    except (ValueError, InspectionError) as exc:
        print(f"mcp-doctor: {exc}", file=sys.stderr)
        return 2

    report = analyze(inspection)
    print(render_json(report) if args.json else render_text(report))
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        raise SystemExit(asyncio.run(_run_inspect(args)))
    parser.error(f"Unknown command: {args.command}")
