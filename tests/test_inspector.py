import asyncio

from fastmcp import FastMCP

from mcp_doctor.inspector import inspect_server


def test_inspects_in_memory_fastmcp_server() -> None:
    server = FastMCP("Test Lab", instructions="Use the greeting tool for introductions.")

    @server.tool
    def greet(name: str) -> str:
        """Greet a person by name and return the complete greeting."""
        return f"Hello, {name}!"

    inspection = asyncio.run(inspect_server(server))

    assert inspection.server_name == "Test Lab"
    assert inspection.instructions == "Use the greeting tool for introductions."
    assert [tool.name for tool in inspection.tools] == ["greet"]
    assert inspection.tools[0].input_schema["required"] == ["name"]
    assert inspection.listing_errors == {}
