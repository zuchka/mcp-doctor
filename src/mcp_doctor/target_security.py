"""Fail-closed target authorization for server-side MCP connections."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


class TargetPolicyError(ValueError):
    """A public target did not satisfy the hosted egress policy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AuthorizedTarget:
    """Validated target data retained for the duration of one inspection."""

    url: str
    origin: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


Resolver = Callable[[str, int], Awaitable[Iterable[str]]]


def _canonical_parts(value: str) -> tuple[str, SplitResult, str, int]:
    if len(value) > 2_048:
        raise TargetPolicyError("invalid_target", "Target URL is too long.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise TargetPolicyError("invalid_target", "Target URL is malformed.") from exc

    if parsed.scheme.lower() != "https":
        raise TargetPolicyError("invalid_target", "Target URL must use HTTPS.")
    if not parsed.hostname:
        raise TargetPolicyError("invalid_target", "Target URL must include a hostname.")
    if parsed.username is not None or parsed.password is not None:
        raise TargetPolicyError("invalid_target", "Target URL cannot contain credentials.")
    if parsed.fragment:
        raise TargetPolicyError("invalid_target", "Target URL cannot contain a fragment.")
    if parsed.query:
        raise TargetPolicyError("invalid_target", "Target URL cannot contain a query string.")
    if port not in {None, 443}:
        raise TargetPolicyError("invalid_target", "Target URL must use the standard HTTPS port.")

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise TargetPolicyError("invalid_target", "Target URL must use a DNS hostname.")

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise TargetPolicyError("invalid_target", "Target hostname is invalid.") from exc

    resolved_port = port or 443
    host_for_netloc = (
        ascii_hostname if resolved_port == 443 else f"{ascii_hostname}:{resolved_port}"
    )
    path = parsed.path or "/"
    canonical = urlunsplit(("https", host_for_netloc, path, "", ""))
    return canonical, parsed, ascii_hostname, resolved_port


def canonical_target_url(value: str) -> str:
    """Return the exact URL representation used by the operator allowlist."""

    canonical, _, _, _ = _canonical_parts(value.strip())
    return canonical


async def _resolve_global_addresses(hostname: str, port: int) -> tuple[str, ...]:
    def lookup() -> set[str]:
        return {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
        }

    try:
        addresses = await asyncio.to_thread(lookup)
    except OSError as exc:
        raise TargetPolicyError(
            "target_unreachable", "Target hostname could not be resolved."
        ) from exc
    return tuple(sorted(addresses))


class TargetPolicy:
    """Authorize exact operator-owned targets and reject non-global DNS answers."""

    def __init__(self, allowed_target_urls: Iterable[str], *, resolver: Resolver | None = None):
        try:
            allowed = tuple(canonical_target_url(item) for item in allowed_target_urls)
        except TargetPolicyError as exc:
            raise ValueError(f"Invalid operator target allowlist: {exc}") from exc
        if not allowed:
            raise ValueError("At least one allowed target URL is required.")
        if len(allowed) != len(set(allowed)):
            raise ValueError("Allowed target URLs must be unique after normalization.")
        self.allowed_target_urls = frozenset(allowed)
        self._resolver = resolver or _resolve_global_addresses

    async def authorize(self, value: str) -> AuthorizedTarget:
        canonical, _, hostname, port = _canonical_parts(value.strip())
        if canonical not in self.allowed_target_urls:
            raise TargetPolicyError(
                "target_not_permitted",
                "Target URL is not approved for this public deployment.",
            )

        addresses = tuple(sorted(set(await self._resolver(hostname, port))))
        if not addresses:
            raise TargetPolicyError("target_unreachable", "Target hostname returned no addresses.")
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address)
            except ValueError as exc:
                raise TargetPolicyError(
                    "target_unreachable", "Target hostname returned an invalid address."
                ) from exc
            if not address.is_global:
                raise TargetPolicyError(
                    "target_not_permitted",
                    "Target hostname does not resolve exclusively to public addresses.",
                )

        origin_port = "" if port == 443 else f":{port}"
        return AuthorizedTarget(
            url=canonical,
            origin=f"https://{hostname}{origin_port}",
            hostname=hostname,
            port=port,
            addresses=addresses,
        )
