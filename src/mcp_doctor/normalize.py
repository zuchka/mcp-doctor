from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from mcp_doctor.models import InventoryItem, ToolDefinition


def model_to_dict(value: Any) -> dict[str, Any]:
    """Turn protocol models into plain data without depending on one SDK release."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    raise TypeError(f"Cannot normalize {type(value).__name__}")


def normalize_tool(tool: Any) -> ToolDefinition:
    payload = model_to_dict(tool)
    return ToolDefinition(
        name=str(payload["name"]),
        title=payload.get("title"),
        description=payload.get("description"),
        input_schema=payload.get("inputSchema") or payload.get("input_schema") or {},
        output_schema=payload.get("outputSchema") or payload.get("output_schema"),
        annotations=payload.get("annotations"),
        meta=payload.get("_meta") or payload.get("meta"),
    )


def normalize_inventory_item(item: Any, *, identifier_keys: tuple[str, ...]) -> InventoryItem:
    payload = model_to_dict(item)
    identifier = next((payload.get(key) for key in identifier_keys if payload.get(key)), None)
    name = payload.get("name") or payload.get("title") or identifier or "<unnamed>"
    return InventoryItem(
        name=str(name),
        identifier=str(identifier) if identifier is not None else None,
        description=payload.get("description"),
    )
