"""Small intentionally imperfect MCP server for trying MCP Doctor locally."""

from fastmcp import FastMCP

mcp = FastMCP("Problematic CRM")


@mcp.tool
def get_customer(customer_id: str) -> dict:
    """GET /customers/{customer_id}."""
    return {"id": customer_id, "name": "Example"}


@mcp.tool
def create_customer(name: str, email: str) -> dict:
    """Create one customer record in the CRM and return its generated identifier."""
    return {"id": "cust_123", "name": name, "email": email}


@mcp.tool
def update_customer(customer_id: str, name: str | None = None) -> dict:
    """Update customer."""
    return {"id": customer_id, "name": name}


@mcp.tool
def delete_customer(customer_id: str) -> bool:
    return True


@mcp.tool
def list_customers(limit: int = 100) -> list[dict]:
    """List customer records, using the same pagination limit exposed by the API endpoint."""
    return []


@mcp.tool
def refundOrder(order_id: str, reason: str) -> dict:
    """Refund a specific order after the user has confirmed the reason for the refund."""
    return {"order_id": order_id, "reason": reason, "status": "refunded"}


if __name__ == "__main__":
    mcp.run()
