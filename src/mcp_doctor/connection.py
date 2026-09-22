from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import Client

from mcp_doctor.models import ServerInspection
from mcp_doctor.normalize import model_to_dict, normalize_inventory_item, normalize_tool


def display_target(source: Any) -> str:
    if isinstance(source, (str, Path)):
        return str(source)
    return type(source).__name__


def error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


@asynccontextmanager
async def open_mcp_client(source: Any, *, timeout: float = 30.0) -> AsyncIterator[Client]:
    client = Client(source, timeout=timeout)
    async with client:
        yield client


async def inspect_connected_client(client: Client, *, target: str) -> ServerInspection:
    server_info = model_to_dict(client.server_info) if client.server_info else {}
    capabilities = model_to_dict(client.server_capabilities) if client.server_capabilities else {}
    inspection = ServerInspection(
        target=target,
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
            inspection.listing_errors[field_name] = error_text(exc)
    return inspection
