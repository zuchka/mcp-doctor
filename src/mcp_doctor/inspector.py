from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp_doctor.connection import (
    display_target,
    error_text,
    inspect_connected_client,
    open_mcp_client,
)
from mcp_doctor.models import ServerInspection


class InspectionError(RuntimeError):
    """Raised when MCP Doctor cannot establish or inspect a connection."""


async def inspect_server(
    source: Any,
    *,
    timeout: float = 30.0,
    target_label: str | None = None,
    fatal_listing_error: Callable[[Exception], bool] | None = None,
) -> ServerInspection:
    """Connect to an MCP server and inspect metadata only; tools are never called."""

    target = target_label or display_target(source)
    try:
        async with open_mcp_client(source, timeout=timeout) as client:
            return await inspect_connected_client(
                client,
                target=target,
                fatal_listing_error=fatal_listing_error,
            )
    except Exception as exc:
        raise InspectionError(f"Could not inspect {target!r}: {error_text(exc)}") from exc
