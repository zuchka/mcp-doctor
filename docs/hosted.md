# Hosted MCP Doctor

MCP Doctor v0.5 adds a separate server for managed, authenticated hosting. It is
deliberately smaller than the trusted local server: the hosted catalog exposes only
`diagnose_remote_mcp_server` and never accepts a local path, process command, MCP config,
header, credential, artifact path, or model setting.

The intended public topology is:

```text
authenticated MCP client
  -> Horizon TLS, OAuth, authorization, and audit
    -> src/mcp_doctor/hosted_server.py:mcp
      -> exact operator-approved HTTPS target
```

Do not expose the FastMCP application directly to the internet. Horizon is the inbound
security boundary; the application is the outbound and result-sanitization boundary.

## Capability

The hosted tool lists a target MCP server's tools, resources, resource templates, and
prompts, then applies MCP Doctor's deterministic interface analysis. It does not invoke a
target tool or call a model. Every result reports `target_tool_calls: 0` and
`model_calls: 0`.

The response is bounded and contains metadata, inventory counts, selected tool names,
metrics, findings, omitted counts, analysis/policy versions, and a correlation ID. It
does not return raw target instructions, full descriptions, deployment internals, or
local paths.

## Required configuration

At runtime, `MCP_DOCTOR_ALLOWED_TARGET_URLS` must be a non-empty JSON array of exact HTTPS
endpoints:

```bash
export MCP_DOCTOR_ALLOWED_TARGET_URLS='["https://example.com/mcp"]'
```

Targets must use a DNS hostname and port 443. Credentials, query strings, fragments, IP
literals, redirects, non-global DNS answers, and URLs not in the exact allowlist are
rejected. Downstream requests do not inherit proxy environment variables or caller
credentials. Response bodies, component counts, component sizes, aggregate catalog size,
schema depth, retained strings, result lists, deadlines, and global concurrency are
bounded.

Horizon imports the entrypoint during image build without runtime variables so it can inspect
the MCP manifest. The module therefore remains importable in that build-only state, but the
tool returns `service_unavailable` before DNS resolution or network I/O. `HostedSettings`
itself remains strict, and Horizon runtime calls become available only after the encrypted
allowlist is injected. This separation preserves fail-closed behavior without blocking
managed manifest inspection.

Optional operator settings are:

| Variable | Default | Absolute ceiling |
| --- | ---: | ---: |
| `MCP_DOCTOR_DEFAULT_TIMEOUT_SECONDS` | 20 | 30 by default |
| `MCP_DOCTOR_MAX_TIMEOUT_SECONDS` | 30 | 60 |
| `MCP_DOCTOR_MAX_CONCURRENCY` | 8 | 100 |
| `MCP_DOCTOR_MAX_RESPONSE_BYTES` | 2,097,152 | 10,000,000 |
| `MCP_DOCTOR_MAX_COMPONENTS_PER_KIND` | 250 | 10,000 |
| `MCP_DOCTOR_MAX_COMPONENT_BYTES` | 131,072 | 10,000,000 |
| `MCP_DOCTOR_MAX_TOTAL_CATALOG_BYTES` | 2,097,152 | 50,000,000 |
| `MCP_DOCTOR_MAX_SCHEMA_DEPTH` | 32 | 128 |
| `MCP_DOCTOR_MAX_PUBLIC_STRING_CHARS` | 500 | 4,000 |
| `MCP_DOCTOR_MAX_FINDINGS` | 25 | 100 |
| `MCP_DOCTOR_MAX_TOOL_NAMES` | 50 | 500 |

Normal tool arguments can only reduce the operation timeout; they cannot disable an
operator limit. The current Horizon plan does not surface operator-configurable
per-principal rate/concurrency limits or audit retention, so keep organization membership
narrow and use the application's global concurrency bound. Keep general egress closed unless
the platform can enforce destination policy against DNS rebinding; the beta's exact allowlist
is the safe fallback.

## Local verification

This command is for loopback smoke testing only:

```bash
MCP_DOCTOR_ALLOWED_TARGET_URLS='["https://example.com/mcp"]' \
  uv run fastmcp run src/mcp_doctor/hosted_server.py:mcp \
  --transport http --host 127.0.0.1 --port 8765 --no-banner
```

Before deploying, run:

```bash
uv sync --frozen --extra eval
uv run pytest -q
uv run ruff check .
uv build
MCP_DOCTOR_ALLOWED_TARGET_URLS='["https://example.com/mcp"]' \
  uv run python -c 'from mcp_doctor.hosted_server import mcp; print(mcp.name)'
```

## Horizon deployment checklist

Deploy the Git revision containing `src/mcp_doctor/hosted_server.py:mcp` from the connected
GitHub repository. Configure only the approved target allowlist and reviewed numeric
limits. No OpenAI key, downstream bearer token, database secret, or shared user credential
is required.

Before describing the endpoint as a public beta, verify all of the following against the
deployed URL:

1. Unauthenticated discovery is rejected by Horizon and browser OAuth works in a supported
   MCP client.
2. Authenticated discovery returns exactly `diagnose_remote_mcp_server`.
3. The approved target succeeds; an unapproved target returns `target_not_permitted` and
   causes no outbound connection.
4. Gateway Host/Origin behavior and tool permissions are tested. Configure per-principal
   rate/concurrency limits and audit retention when the current plan exposes them; until then,
   record the limitation and keep membership narrow.
5. The audit event identifies the caller, tool, build/deployment, duration, and outcome
   without leaking secrets. If the platform does not index the application correlation ID,
   correlate by time and the Horizon request ID without re-enabling payload logging.
6. A current required MCP conformance run has no unexplained failure.
7. Discovery and invocation work after scale-to-zero, and a previous known-good deployment
   has been restored once to prove rollback.

Record evidence and failed attempts in
[`docs/v0.5-horizon-friction-log.md`](v0.5-horizon-friction-log.md). Do not put endpoint
tokens, cookies, authorization codes, private URLs, or unredacted user data in that file.

## Public errors

Expected failures are returned as one of:

- `invalid_target`
- `target_not_permitted`
- `target_auth_required`
- `target_unreachable`
- `target_timeout`
- `target_too_large`
- `target_protocol_error`
- `service_unavailable`

Unexpected failures become a masked tool error containing only a caller-safe correlation
ID. Operators should use the same ID to inspect Horizon logs. Do not copy an internal
exception, request header, or target payload into a client response.

## Beta limitations

- Targets are an operator-owned exact allowlist, not arbitrary user-supplied URLs.
- Targets that require downstream authentication are unsupported.
- Hosted comparisons, persisted reports, representative-task evals, Scope Lab, local files,
  and process execution remain available only through the trusted local server.
- The application has global backpressure; identity-aware throttling and inbound host/origin
  enforcement depend on Horizon. The current plan does not expose operator-configurable
  per-principal throttling or audit retention.
- The npm MCP conformance runner available during implementation did not yet contain the
  final 2026-07-28 requirement set. This is tracked as deployment friction, not silently
  treated as a pass.

## Rollback and incident response

If authentication, authorization, target policy, limits, result redaction, or audit fails,
stop advertising the endpoint and restore the recorded v0.4 revision. Remove any unsafe
target from the allowlist before retrying. Preserve sanitized build and audit evidence,
record an entry in the friction log, and do not re-open access until the failed invariant
has an explicit verification result.

## Verified beta deployment

As of 2026-09-22, the authenticated beta is Live at
`https://mcp-doctor-beta.fastmcp.app/mcp` from merge `3aca4505`. Production discovery exposes
one hosted-safe tool. Approved and denied calls, anonymous access, forged Host handling,
privacy-preserving audit metadata, cold connection, and rollback/restoration have been tested.
See the friction log for the sanitized evidence and remaining launch limitations.
