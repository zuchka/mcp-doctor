from mcp_doctor.normalize import normalize_inventory_item, normalize_tool


def test_normalizes_protocol_aliases() -> None:
    tool = normalize_tool(
        {
            "name": "search_orders",
            "description": "Search orders using customer-facing filters.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "outputSchema": {"type": "array"},
            "_meta": {"owner": "support"},
        }
    )

    assert tool.name == "search_orders"
    assert tool.input_schema["required"] == ["query"]
    assert tool.output_schema == {"type": "array"}
    assert tool.meta == {"owner": "support"}


def test_normalizes_resource_identifier() -> None:
    item = normalize_inventory_item(
        {"name": "handbook", "uri": "docs://handbook"}, identifier_keys=("uri",)
    )
    assert item.name == "handbook"
    assert item.identifier == "docs://handbook"
