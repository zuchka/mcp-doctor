"""Horizon entrypoint for the fail-closed public MCP surface."""

from mcp_doctor.hosted import create_hosted_server
from mcp_doctor.hosted_settings import HostedSettings

mcp = create_hosted_server(HostedSettings.from_env())
