"""Small deterministic server for trying MCP Doctor V0.3 evals."""

from fastmcp import FastMCP

mcp = FastMCP("Example CRM", instructions="Use account tools only for the requested customer.")


@mcp.tool
def lookup_customer(email: str) -> dict[str, str]:
    """Use when an exact email address must be resolved to a customer ID."""
    return {"customer_id": "C-42", "email": email, "name": "Ada Lovelace"}


@mcp.tool
def lookup_orders(customer_id: str) -> list[dict[str, str]]:
    """Use after resolving a customer ID to retrieve that customer's orders."""
    return [{"order_id": "A-123", "status": "shipped", "customer_id": customer_id}]


@mcp.tool
def health_check() -> dict[str, str]:
    """Use only to check service availability, not for customer or order questions."""
    return {"status": "ok"}


@mcp.tool
def add_customer_note(customer_id: str, note: str) -> dict[str, str]:
    """Use when the user explicitly asks to persist a note on a customer record."""
    return {"customer_id": customer_id, "note": note, "status": "created"}


if __name__ == "__main__":
    mcp.run()
