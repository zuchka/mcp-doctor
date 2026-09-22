# MCP Doctor

MCP Doctor is a quality lab for MCP servers. It connects as an MCP client, inventories the
interface an agent sees, reports deterministic interface smells, and can run representative
agent tasks against the live server.

This is intentionally **not** an MCP conformance suite. It asks two focused questions: *is this
tool surface legible, economical, and unambiguous for an agent, and can an agent reliably use it
to complete the work users actually request?* `inspect` never calls server tools. `eval` does,
inside an explicit safety and recording boundary.

## V0.3

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
- Detect semantically overlapping tool pairs using deterministic lexical signals.
- Check descriptions for explicit positive and contrasting selection guidance.
- Load TOML policies with configurable thresholds, rule selection, and auditable suppressions.
- Save versioned JSON reports and compare them as CI baselines.
- Render a readable terminal report or JSON for automation.
- Define versioned representative-task suites using stable capability IDs.
- Map concrete server tools to capabilities and read/write/destructive effects by revision.
- Run an OpenAI Responses agent against real MCP tool calls in isolated, sequential attempts.
- Apply deterministic graders for capability coverage, forbidden calls, budgets, ordering,
  final-answer contents or regexes, and structured tool results.
- Measure success, irrelevant and forbidden calls, tool errors, call count, latency, token use,
  and tool-definition context size.
- Save versioned eval runs and compare compatible revisions with regression-aware CI exits.

## Try it

```bash
uv sync
uv run mcp-doctor inspect examples/problematic_server.py
uv run mcp-doctor inspect examples/ambiguous_server.py
uv run mcp-doctor inspect https://your-server.example/mcp
uv run mcp-doctor inspect https://your-server.example/mcp --json
uv run mcp-doctor inspect --config ./mcp.json
uv run mcp-doctor inspect examples/ambiguous_server.py \
  --policy examples/mcp-doctor.toml \
  --save-report current.json
```

The local example is deliberately imperfect, so the report has useful findings.
The config form accepts FastMCP's `mcpServers` JSON shape and covers command-based STDIO,
headers, and other explicit transport settings. Config files can launch programs, so inspect
only configs you trust.

`--config` configures the MCP connection. `--policy` separately configures Doctor's analysis.
Saved reports are always versioned JSON, while `--json` only selects the stdout format.

## Representative task evals

Eval support uses the optional OpenAI dependency and requires an API key for a real run:

```bash
uv sync --extra eval
export OPENAI_API_KEY=...

# Validate inventory and contracts without model or server-tool calls.
uv run mcp-doctor eval examples/eval_server.py \
  --suite examples/evals/crm-suite.toml \
  --capability-map examples/evals/crm-v1.toml \
  --dry-run

# Run the suite and save a versioned artifact. This makes billable model calls.
uv run mcp-doctor eval examples/eval_server.py \
  --suite examples/evals/crm-suite.toml \
  --capability-map examples/evals/crm-v1.toml \
  --provider openai \
  --model YOUR_MODEL_ID \
  --repetitions 3 \
  --save-run eval-v1.json \
  --fail-on-task-failure
```

The suite defines user prompts, stable capability IDs, expected/allowed/forbidden capabilities,
optional expected ordering, tags, limits, and deterministic graders. The separate capability map
binds one server revision's concrete tool names to those capabilities and declares every tool's
effect. Preflight rejects missing, stale, or unknown mappings, so an interface cannot silently
drift away from the eval contract.

Use `--task ID` or `--tag TAG` repeatedly to select a subset. Attempts run sequentially and each
gets a fresh MCP connection and model session. A task or tool timeout, turn limit, tool-call
budget, or provider failure becomes structured attempt data instead of losing the whole run.

Eval runs are read-only by default. A mapped `write` or `destructive` call is returned to the
agent as blocked unless the run includes `--allow-writes` or `--allow-destructive`, respectively.
Review both the suite and capability map before granting either permission. `unknown` effects are
always blocked by the CLI.

The default `--record metadata` stores call identity, classification, status, size, and timing but
omits tool arguments and results. Use `--record redacted --redact-pattern REGEX` to retain payloads
after regex replacement, or `--record full` only when the data is safe to persist. User prompts
and final answers remain part of the eval artifact in every mode. The complete suite, capability
map, server inspection, fingerprints, agent settings, and harness settings are also embedded for
auditability.

The first provider adapter uses OpenAI Responses custom-function tools. The harness and graders
are provider-neutral, and the included scripted driver keeps tests and local harness development
offline and deterministic.

## Selection analysis

V0.2 splits names written as snake_case, kebab-case, or camelCase and compares normalized tool
names, descriptions, and parameter vocabulary. A small synonym table treats intents such as
`find`, `search`, and `lookup` consistently. The score is deterministic and explainable; reports
show the component scores and shared concepts rather than presenting the heuristic as semantic
truth.

Descriptions should say when an agent should choose a tool. Use explicit phrases such as “Use
when…” or “Best for…”. When two tools overlap, add a contrast such as “Do not use for…” or “Use
search_orders instead…”. Different state-changing actions such as create and delete are kept
distinct even when they operate on the same entity.

No analyzer calls an LLM, invokes a server tool, or uses embeddings.

## Analysis policy

Policies are strict TOML files. Unrecognized keys and invalid ranges are errors. All fields are
optional except the policy version, so a file can override only the settings it needs:

```toml
version = 1
disabled_rules = ["crud-wrapper-smells"]

[thresholds]
tool_count = 20
total_definition_tokens = 8000
tool_definition_tokens = 2000
parameter_count = 10
schema_depth = 4
union_branches = 5
description_min_chars = 30
description_max_chars = 800
semantic_overlap = 0.72

[[suppressions]]
rule = "semantic-overlap.ambiguous-pair"
tools = ["find_orders", "search_orders"]
reason = "Intentional aliases while clients migrate to search_orders."
```

`enabled_rules` and `disabled_rules` accept either a complete rule ID or a check prefix such as
`semantic-overlap`. Suppressions use exact rule IDs plus an exact tool set or stable subject.
Every suppression needs a reason, remains visible in reports, and does not count as an active
warning.

## Baselines and CI

Create a baseline and a current report with the same policy:

```bash
uv run mcp-doctor inspect path/to/server.py \
  --policy mcp-doctor.toml \
  --save-report baseline.json

uv run mcp-doctor inspect path/to/server.py \
  --policy mcp-doctor.toml \
  --save-report current.json

uv run mcp-doctor diff baseline.json current.json --fail-on-new
```

The diff classifies new, resolved, unchanged, newly suppressed, and newly active findings. It
also shows tool additions/removals, metric changes, and policy changes. Finding fingerprints do
not depend on prose or numeric evidence, so message edits do not create false regressions.

Eval runs can be compared when they use the same suite fingerprint, selected tasks, provider,
model, agent settings, repetition count, allowed effects, and instructions:

```bash
uv run mcp-doctor eval-diff eval-v1.json eval-v2.json --fail-on-regression
```

The comparison reports task-level success-rate, median call-count, irrelevant-call, latency, and
input-token deltas. A lower success rate, more forbidden calls, or more tool errors is a
regression. Capability maps and server interface fingerprints remain in each artifact, allowing
the concrete server revision to change while the user-task contract stays fixed.

Exit codes are `0` for success; `1` when an explicitly requested CI gate fails
(`--fail-on-new`, `--fail-on-task-failure`, or `--fail-on-regression`); and `2` for invalid input,
connection/preflight failures, invalid policies or eval contracts, incompatible eval runs, or
unreadable artifacts. Existing warnings do not fail `inspect`, and failed eval tasks do not fail
the command unless `--fail-on-task-failure` is present.

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

The token number is deliberately approximate. MCP Doctor serializes each normalized definition as
compact JSON and divides its byte length by four. Actual tokenization varies by model, but the
estimate is deterministic and useful for comparing two versions of the same server.

Every warning is a heuristic, not a verdict. `get_customer` may be exactly the right tool in a
small server. A full CRUD family, large catalog, and endpoint-oriented descriptions together
are stronger evidence that the server mirrors an API instead of curating agent workflows.

## Shape of the code

```text
FastMCP Client
    -> connection.py     shared MCP connection and capability listing
    -> inspector.py      read-only static inspection boundary
    -> normalize.py      MCP models to stable Pydantic models
    -> semantic.py       deterministic similarity and guidance signals
    -> analyzers.py      pure deterministic checks
    -> policy.py         thresholds, rule filters, and suppressions
    -> diff.py           versioned report persistence and comparison
    -> report.py         text and JSON presentation
    -> evals/
       -> suite.py       suite/map loading, fingerprints, preflight validation
       -> drivers/       provider contract, OpenAI Responses, scripted tests
       -> harness.py     isolated tasks and MCP tool execution with safety gates
       -> grading.py     deterministic task graders
       -> metrics.py     task/run aggregation and context estimates
       -> io.py          versioned eval-run persistence
       -> compare.py     compatible-run regression comparison
       -> report.py      eval text and JSON presentation
    -> cli.py            command-line boundary
```

The analyzers are pure functions so their thresholds and false positives are easy to test and
debate—useful both for learning and for an interview walkthrough.

## Roadmap

### V0.2 — richer static analysis — complete

- semantic overlap and ambiguous tool-selection pairs;
- explicit positive/negative selection guidance in descriptions;
- saved baselines and report diffs for CI;
- configurable thresholds and suppressions.

### V0.3 — representative task evals — complete

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
