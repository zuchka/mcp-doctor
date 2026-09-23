import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator

import httpx2
import pytest
from fastmcp import Client

from mcp_doctor.analyzers import analyze
from mcp_doctor.hosted import (
    HostedLimitError,
    _ByteLimitedStream,
    _contains_exception,
    _limited_client_factory,
    create_hosted_server,
)
from mcp_doctor.hosted_settings import HostedConfigurationError, HostedSettings
from mcp_doctor.inspector import InspectionError, inspect_server
from mcp_doctor.models import ServerInspection, ToolDefinition
from mcp_doctor.target_security import TargetPolicy, TargetPolicyError, canonical_target_url

ALLOWED = "https://example.com/mcp"


async def _public_resolver(hostname: str, port: int) -> list[str]:
    assert hostname == "example.com"
    assert port == 443
    return ["8.8.8.8"]


def _settings(**updates: object) -> HostedSettings:
    return HostedSettings(allowed_target_urls=(ALLOWED,), **updates)


def _inspection(*, tools: list[ToolDefinition] | None = None) -> ServerInspection:
    return ServerInspection(
        target=ALLOWED,
        server_name="Example",
        server_version="1.0",
        protocol_version="2026-07-28",
        tools=tools or [],
    )


def test_hosted_settings_fail_closed_and_parse_json_allowlist() -> None:
    with pytest.raises(HostedConfigurationError, match="required"):
        HostedSettings.from_env({})
    with pytest.raises(HostedConfigurationError, match="JSON array"):
        HostedSettings.from_env({"MCP_DOCTOR_ALLOWED_TARGET_URLS": ALLOWED})

    settings = HostedSettings.from_env(
        {
            "MCP_DOCTOR_ALLOWED_TARGET_URLS": json.dumps([ALLOWED]),
            "MCP_DOCTOR_MAX_CONCURRENCY": "3",
        }
    )
    assert settings.allowed_target_urls == (ALLOWED,)
    assert settings.max_concurrency == 3


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com/mcp",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp#fragment",
        "https://example.com/mcp?token=secret",
        "https://127.0.0.1/mcp",
        "https://[::1]/mcp",
        "https://example.com:8443/mcp",
    ],
)
def test_target_url_rejects_unsafe_shapes(value: str) -> None:
    with pytest.raises(TargetPolicyError, match="Target"):
        canonical_target_url(value)


def test_target_policy_requires_exact_allowlist_and_public_dns() -> None:
    resolver_called = False

    async def private_resolver(hostname: str, port: int) -> list[str]:
        nonlocal resolver_called
        resolver_called = True
        return ["127.0.0.1"]

    async def check() -> None:
        nonlocal resolver_called
        policy = TargetPolicy((ALLOWED,), resolver=private_resolver)
        with pytest.raises(TargetPolicyError) as denied:
            await policy.authorize("https://other.example/mcp")
        assert denied.value.code == "target_not_permitted"
        assert not resolver_called

        with pytest.raises(TargetPolicyError) as private:
            await policy.authorize(ALLOWED)
        assert private.value.code == "target_not_permitted"
        assert resolver_called

        authorized = await TargetPolicy((ALLOWED,), resolver=_public_resolver).authorize(ALLOWED)
        assert authorized.url == ALLOWED
        assert authorized.origin == "https://example.com"
        assert authorized.addresses == ("8.8.8.8",)

        mixed_policy = TargetPolicy(
            (ALLOWED,), resolver=lambda hostname, port: _mixed_addresses(hostname, port)
        )
        with pytest.raises(TargetPolicyError) as mixed:
            await mixed_policy.authorize(ALLOWED)
        assert mixed.value.code == "target_not_permitted"

    asyncio.run(check())


async def _mixed_addresses(hostname: str, port: int) -> list[str]:
    assert hostname == "example.com"
    assert port == 443
    return ["8.8.8.8", "::ffff:127.0.0.1"]


def test_hosted_surface_is_narrow_and_passes_doctor_analysis() -> None:
    async def check() -> None:
        server = create_hosted_server(_settings(), resolver=_public_resolver)
        inspection = await inspect_server(server)
        assert [tool.name for tool in inspection.tools] == ["diagnose_remote_mcp_server"]
        schema_text = json.dumps(inspection.tools[0].input_schema, sort_keys=True)
        assert inspection.tools[0].input_schema["additionalProperties"] is False
        for forbidden in ("path", "header", "credential", "token", "command"):
            assert forbidden not in schema_text.lower()
        annotations = inspection.tools[0].annotations or {}
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["idempotentHint"] is True
        assert annotations["openWorldHint"] is True
        assert analyze(inspection).warning_count == 0

    asyncio.run(check())


def test_hosted_response_stream_stops_at_wire_byte_limit() -> None:
    class Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"abc", b"d"):
                yield chunk

    async def check() -> None:
        stream = _ByteLimitedStream(Chunks(), 3)
        with pytest.raises(HostedLimitError, match="too large"):
            _ = [chunk async for chunk in stream]

    asyncio.run(check())


def test_hosted_limit_detection_handles_exception_groups() -> None:
    grouped = ExceptionGroup(
        "transport task group", [RuntimeError("noise"), HostedLimitError("too large")]
    )
    assert _contains_exception(grouped, HostedLimitError)


def test_hosted_http_client_strips_credentials_and_rejects_unsafe_bodies() -> None:
    async def check() -> None:
        client = _limited_client_factory(
            3,
            headers={"Authorization": "Bearer secret", "Cookie": "session=secret"},
            follow_redirects=True,
        )
        try:
            assert "authorization" not in client.headers
            assert "cookie" not in client.headers
            assert client.headers["accept-encoding"] == "identity"
            assert client.follow_redirects is False
            hook = client.event_hooks["response"][0]
            request = httpx2.Request("POST", ALLOWED)
            with pytest.raises(HostedLimitError, match="too large"):
                await hook(httpx2.Response(200, headers={"content-length": "4"}, request=request))
            with pytest.raises(HostedLimitError, match="compression"):
                await hook(
                    httpx2.Response(200, headers={"content-encoding": "gzip"}, request=request)
                )
        finally:
            await client.aclose()

    asyncio.run(check())


def test_hosted_diagnosis_returns_bounded_structured_result() -> None:
    seen: dict[str, object] = {}

    async def fake_inspector(source: object, **kwargs: object) -> ServerInspection:
        seen["source"] = source
        seen.update(kwargs)
        return _inspection(
            tools=[
                ToolDefinition(
                    name="lookup\x00_customer",
                    description="Use when resolving a customer from an exact email address.",
                    input_schema={
                        "type": "object",
                        "properties": {"email": {"type": "string"}},
                        "required": ["email"],
                    },
                )
            ]
        )

    async def check() -> None:
        server = create_hosted_server(
            _settings(), resolver=_public_resolver, inspector=fake_inspector
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "diagnose_remote_mcp_server",
                {"target_url": ALLOWED, "policy_profile": "default"},
            )
        data = result.structured_content
        assert data is not None
        assert data["status"] == "completed"
        assert data["target_origin"] == "https://example.com"
        assert data["tool_names"] == ["lookup_customer"]
        assert data["inventory_counts"]["tools"] == 1
        assert data["target_tool_calls"] == 0
        assert data["model_calls"] == 0
        assert "source" in seen
        assert seen["target_label"] == "https://example.com"
        assert callable(seen["fatal_listing_error"])

    asyncio.run(check())


def test_hosted_diagnosis_rejects_unapproved_target_without_connecting() -> None:
    called = False

    async def forbidden_inspector(source: object, **kwargs: object) -> ServerInspection:
        nonlocal called
        called = True
        return _inspection()

    async def check() -> None:
        server = create_hosted_server(
            _settings(), resolver=_public_resolver, inspector=forbidden_inspector
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "diagnose_remote_mcp_server",
                {"target_url": "https://other.example/mcp"},
            )
        data = result.structured_content
        assert data is not None
        assert data["status"] == "failed"
        assert data["error_code"] == "target_not_permitted"
        assert not called

    asyncio.run(check())


def test_hosted_diagnosis_enforces_catalog_limits() -> None:
    async def oversized_inspector(source: object, **kwargs: object) -> ServerInspection:
        return _inspection(
            tools=[
                ToolDefinition(name="first", input_schema={"type": "object"}),
                ToolDefinition(name="second", input_schema={"type": "object"}),
            ]
        )

    async def check() -> None:
        server = create_hosted_server(
            _settings(max_components_per_kind=1),
            resolver=_public_resolver,
            inspector=oversized_inspector,
        )
        async with Client(server) as client:
            result = await client.call_tool("diagnose_remote_mcp_server", {"target_url": ALLOWED})
        data = result.structured_content
        assert data is not None
        assert data["status"] == "failed"
        assert data["error_code"] == "target_too_large"

    asyncio.run(check())


def test_hosted_diagnosis_maps_wrapped_response_limit() -> None:
    async def oversized_response_inspector(source: object, **kwargs: object) -> ServerInspection:
        try:
            raise HostedLimitError("untrusted oversized detail")
        except HostedLimitError as exc:
            raise InspectionError("wrapped transport failure") from exc

    async def check() -> None:
        server = create_hosted_server(
            _settings(), resolver=_public_resolver, inspector=oversized_response_inspector
        )
        async with Client(server) as client:
            result = await client.call_tool("diagnose_remote_mcp_server", {"target_url": ALLOWED})
        data = result.structured_content
        assert data is not None
        assert data["error_code"] == "target_too_large"
        assert "untrusted" not in json.dumps(data)

    asyncio.run(check())


def test_hosted_diagnosis_times_out_and_cancels_inspection() -> None:
    cancelled = False

    async def slow_inspector(source: object, **kwargs: object) -> ServerInspection:
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return _inspection()

    async def check() -> None:
        server = create_hosted_server(
            _settings(default_timeout_seconds=0.01, max_timeout_seconds=0.02),
            resolver=_public_resolver,
            inspector=slow_inspector,
        )
        async with Client(server) as client:
            result = await client.call_tool("diagnose_remote_mcp_server", {"target_url": ALLOWED})
        data = result.structured_content
        assert data is not None
        assert data["error_code"] == "target_timeout"
        assert cancelled

    asyncio.run(check())


def test_hosted_diagnosis_maps_target_auth_without_exposing_details() -> None:
    async def auth_inspector(source: object, **kwargs: object) -> ServerInspection:
        request = httpx2.Request("POST", ALLOWED)
        response = httpx2.Response(401, request=request)
        cause = httpx2.HTTPStatusError(
            "secret downstream detail", request=request, response=response
        )
        try:
            raise cause
        except httpx2.HTTPStatusError as exc:
            raise InspectionError("wrapped secret") from exc

    async def check() -> None:
        server = create_hosted_server(
            _settings(), resolver=_public_resolver, inspector=auth_inspector
        )
        async with Client(server) as client:
            result = await client.call_tool("diagnose_remote_mcp_server", {"target_url": ALLOWED})
        data = result.structured_content
        assert data is not None
        assert data["error_code"] == "target_auth_required"
        assert "secret" not in json.dumps(data)

    asyncio.run(check())


def test_unconfigured_hosted_server_is_discoverable_but_cannot_connect() -> None:
    called = False

    async def forbidden_inspector(source: object, **kwargs: object) -> ServerInspection:
        nonlocal called
        called = True
        return _inspection()

    async def check() -> None:
        server = create_hosted_server(None, inspector=forbidden_inspector)
        inspection = await inspect_server(server)
        assert [tool.name for tool in inspection.tools] == ["diagnose_remote_mcp_server"]
        async with Client(server) as client:
            result = await client.call_tool(
                "diagnose_remote_mcp_server",
                {"target_url": ALLOWED},
            )
        data = result.structured_content
        assert data is not None
        assert data["status"] == "failed"
        assert data["error_code"] == "service_unavailable"
        assert not called

    asyncio.run(check())


def test_hosted_entrypoint_import_supports_build_inspection_without_allowlist() -> None:
    environment = os.environ.copy()
    environment.pop("MCP_DOCTOR_ALLOWED_TARGET_URLS", None)
    missing = subprocess.run(
        [sys.executable, "-c", "from mcp_doctor.hosted_server import mcp; print(mcp.name)"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert missing.returncode == 0
    assert missing.stdout.strip() == "MCP Doctor Public"

    environment["MCP_DOCTOR_ALLOWED_TARGET_URLS"] = json.dumps([ALLOWED])
    configured = subprocess.run(
        [sys.executable, "-c", "from mcp_doctor.hosted_server import mcp; print(mcp.name)"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert configured.returncode == 0
    assert configured.stdout.strip() == "MCP Doctor Public"
