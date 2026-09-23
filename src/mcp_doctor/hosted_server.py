"""Horizon entrypoint for the fail-closed public MCP surface."""

import logging

from mcp_doctor.hosted import create_hosted_server
from mcp_doctor.hosted_settings import HostedConfigurationError, HostedSettings

_logger = logging.getLogger(__name__)

try:
    _settings = HostedSettings.from_env()
except HostedConfigurationError:
    # Horizon inspects the entrypoint while building without injecting runtime variables.
    # The resulting manifest is safe: calls return service_unavailable before DNS or I/O.
    _logger.warning("Hosted configuration unavailable; using fail-closed manifest mode.")
    _settings = None

mcp = create_hosted_server(_settings)
