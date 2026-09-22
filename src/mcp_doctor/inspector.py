from __future__ import annotations

from pathlib import Path
from typing import Any

from fastmcp import Client

from mcp_doctor.models import ServerInspection
from mcp_doctor.normalize import model_to_dict, normalize_inventory_item, normalize_tool


class InspectionError(RuntimeError):
    """Raised when MCP Doctor cannot establish or inspect a connection."""


def _display_target(source: Any) -> str:
    if isinstance(source, (str, Path)):
        return str(source)
    return type(source).__name__


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def inspect_server(
    source: Any, *, timeout: float = 30.0, target_label: str | None = None
) -> ServerInspection:
    """Connect to an MCP server and inspect metadata only; tools are never called."""

    display_target = target_label or _display_target(source)
    try:
        client = Client(source, timeout=timeout)
        async with client:
            server_info = model_to_dict(client.server_info) if client.server_info else {}
            capabilities = (
                model_to_dict(client.server_capabilities) if client.server_capabilities else {}
            )
            inspection = ServerInspection(
                target=display_target,
                server_name=server_info.get("name"),
                server_version=server_info.get("version"),
                protocol_version=(
                    str(client.protocol_version) if client.protocol_version is not None else None
                ),
                instructions=client.instructions,
                capabilities=capabilities,
            )

            listings = (
                ("tools", client.list_tools, normalize_tool, {}),
                (
                    "resources",
                    client.list_resources,
                    normalize_inventory_item,
                    {"identifier_keys": ("uri",)},
                ),
                (
                    "resource_templates",
                    client.list_resource_templates,
                    normalize_inventory_item,
                    {"identifier_keys": ("uriTemplate", "uri_template")},
                ),
                (
                    "prompts",
                    client.list_prompts,
                    normalize_inventory_item,
                    {"identifier_keys": ("name",)},
                ),
            )

            for field_name, list_operation, normalizer, normalizer_kwargs in listings:
                try:
                    items = await list_operation()
                    setattr(
                        inspection,
                        field_name,
                        [normalizer(item, **normalizer_kwargs) for item in items],
                    )
                except Exception as exc:  # A partial report is more useful than no report.
                    inspection.listing_errors[field_name] = _error_text(exc)

            return inspection
    except Exception as exc:
        raise InspectionError(f"Could not inspect {display_target!r}: {_error_text(exc)}") from exc
