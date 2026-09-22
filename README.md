# MCP Doctor

MCP Doctor is a small, deterministic quality lab for MCP servers. It connects as an MCP
client, inventories the interface an agent sees, normalizes tool definitions, and reports
interface smells before an LLM is involved.

This is intentionally **not** an MCP conformance suite and it does not call server tools.
V0.1 asks a narrower question: *is this tool surface legible and economical for an agent?*

## V0.1

- Connect to a Streamable HTTP endpoint or a local Python/JavaScript MCP server.
- Report negotiated server metadata and tools, resources, resource templates, and prompts.
- Normalize SDK protocol models into stable Pydantic models.
- Run deterministic checks for:
  - tool count;
  - serialized schema size and estimated token footprint;
  - parameter count, nesting, and union complexity;
  - missing, very short, or very long descriptions;
  - mixed or nonstandard names;
  - CRUD families and endpoint-shaped descriptions.
- Render a readable terminal report or JSON for automation.

## Try it

```bash
uv sync
uv run mcp-doctor inspect examples/problematic_server.py
uv run mcp-doctor inspect https://your-server.example/mcp
uv run mcp-doctor inspect https://your-server.example/mcp --json
uv run mcp-doctor inspect --config ./mcp.json
```

The local example is deliberately imperfect, so the report has useful findings.
The config form accepts FastMCP's `mcpServers` JSON shape and covers command-based STDIO,
headers, and other explicit transport settings. Config files can launch programs, so inspect
only configs you trust.

Run the verification suite with:

```bash
uv run pytest
uv run ruff check .
```

## What the client is learning

An MCP connection starts with protocol negotiation. The client learns the server identity,
instructions, and advertised capabilities, then uses separate list operations to discover
tools, resources, resource templates, and prompts. Those protocol objects are the exact
interface an agent client receives.

MCP Doctor converts each tool into a small internal model before analyzing it. That boundary
is important: protocol/SDK versions can add fields, while the quality checks should operate on
a stable representation we control.

The token number is deliberately approximate. V0.1 serializes each normalized definition as
compact JSON and divides its byte length by four. Actual tokenization varies by model, but the
estimate is deterministic and useful for comparing two versions of the same server.

Every warning is a heuristic, not a verdict. `get_customer` may be exactly the right tool in a
small server. A full CRUD family, large catalog, and endpoint-oriented descriptions together
are stronger evidence that the server mirrors an API instead of curating agent workflows.

## Shape of the code

```text
FastMCP Client
    -> inspector.py      protocol negotiation and capability listing
    -> normalize.py      MCP models to stable Pydantic models
    -> analyzers.py      pure deterministic checks
    -> report.py         text and JSON presentation
    -> cli.py            command-line boundary
```

The analyzers are pure functions so their thresholds and false positives are easy to test and
debate—useful both for learning and for an interview walkthrough.

## Roadmap

### V0.2 — richer static analysis

- semantic overlap and ambiguous tool-selection pairs;
- explicit positive/negative selection guidance in descriptions;
- saved baselines and report diffs for CI;
- configurable thresholds and suppressions.

### V0.3 — representative task evals

- define user tasks and expected capabilities;
- run an agent against the server in a controlled harness;
- measure success, irrelevant calls, call count, latency, and context cost;
- compare server-interface revisions against the same eval set.

### V0.4 — MCP Doctor as an MCP server

Expose inspection and evaluation as task-oriented tools so coding agents can diagnose MCP
servers while developing them.

### V0.5 — deployment and orchestration

- deploy the Doctor server behind Horizon for identity, policy, and audit controls;
- add Prefect only when eval suites become durable, concurrent workflows that benefit from
  retries, caching, observability, scheduling, or distributed execution.

The sequencing is deliberate: first prove the inspection model, then add LLM judgment, then
operationalize work whose reliability requirements have become real.
