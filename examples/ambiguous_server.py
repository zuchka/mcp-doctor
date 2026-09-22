"""MCP server with an intentionally ambiguous tool pair for trying V0.2 checks."""

from fastmcp import FastMCP

mcp = FastMCP("Ambiguous Orders")


@mcp.tool
def find_orders(query: str) -> list[dict]:
    """Use when searching orders from free-form customer evidence and account details."""
    return []


@mcp.tool
def search_orders(query: str) -> list[dict]:
    """Use when searching orders from free-form customer evidence and account details."""
    return []


if __name__ == "__main__":
    mcp.run()
