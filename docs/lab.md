# Scope Lab

Scope Lab runs a pinned 2×2 Supabase Evals study: broad or scoped MCP features, each
with or without two preinstalled Supabase skills. Supabase Evals executes tasks and
scores task state. Doctor validates the setup, runs attempts in seeded four-cell
blocks, records provenance, and summarizes the results. Doctor's native `eval`
suite and graders are separate.

## Prepare the pilot

1. Use committed Doctor and Supabase Evals checkouts. Record their 40-character
   commits, Doctor version, the exact Supabase MCP server version/build, and the
   model/harness settings. Use ephemeral platform-lite state. Never put a project
   URL, access token, or other credential in the manifest or plan.
2. In the pinned Evals checkout, define four experiments with identical agent and
   runtime settings. Set only `supabaseMcpServer({features: ...})` and `skills`
   differently. Generate `scope-lab-contract.json` from the runner's resolved
   configuration. The contract format is below. Doctor refuses to infer these
   settings from TypeScript or console output.
3. Produce two saved Doctor format-1 static reports from the broad and scoped
   Supabase MCP surfaces. The reports must reflect the same exact server build as
   the contract. Keep report JSON and the Evals checkout local.
4. Copy [the pilot manifest](../examples/lab/supabase-scope-pilot.toml), replace
   placeholders, and set paths relative to that manifest. The three tasks are
   candidates; preflight validates availability and platform-lite support.

## Companion contract

Doctor reads `<evals_checkout>/scope-lab-contract.json`. The companion Evals code
must export this from resolved runtime configuration, not from handwritten claims.
The JSON has `version: 1`, `result_schema_version: 1`, `run_index_base: 1`, `evals_commit`,
`skill_commit`, a `tasks` array, and an `experiments` array. Each task entry has
`id`, `interface`, `runtime_supported`, and `skills_override`. Each experiment
entry has:

```json
{
  "id": "exact-experiment-id",
  "agent": {
    "harness": "codex",
    "model": "exact-model-id",
    "reasoning_effort": "medium"
  },
  "runtime": {"kind": "platform-lite", "ephemeral": true, "settings": {}},
  "features": ["docs", "database"],
  "skills": ["supabase", "supabase-postgres-best-practices"],
  "mcp_server_version": "exact-version",
  "tool_names": ["resolved", "tool", "names"],
  "tool_catalog_sha256": "64-lowercase-hex-characters",
  "config_sha256": "64-lowercase-hex-characters"
}
```

The catalog hash is SHA-256 of canonical JSON: the full normalized Doctor tool
objects sorted by name, JSON object keys sorted, compact separators, UTF-8, and
one trailing newline. The companion runner must calculate `config_sha256` over
the resolved settings it will actually launch and echo it in each result.

Result schema 1 preserves the current Evals fields: `experiment`, `eval`, 1-based
`run`, `interface: "mcp"`, `passed: boolean`, `checks` as an array of named
pass/fail entries, `skills.available`, `skills.loaded`, and `experimentDisplay`
with `agent`, `modelProvider`, `modelId`, and `reasoningEffort`. The companion
extension must add `scopeLab` with `version: 1`, `configSha256`,
`mcpServerVersion`, `features`, `skills`, `runtime`, and a unique `sandboxId`
for fresh ephemeral state. These provenance fields must describe the runtime
actually launched, not merely copy the authored settings.

Optional Evals measurements are `docs.calls`, `stepCount`, `toolCallCount`,
`agentRunDurationMs`, and `usage` as an array of per-model entries containing
`model`, `inputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`, and
`outputTokens`. Doctor retains safe docs aggregates but not queries, URLs, or
page content in its normalized/public artifacts. Cache reads and writes are
subsets of input. Evals does not expose a separate reasoning-token count, so
Doctor leaves that metric unavailable rather than treating it as zero.

An upstream schema change requires a new contract and importer version. A missing
or incompatible contract stops preflight before a billable run.

## Commands

```bash
mcp-doctor compare broad.json scoped.json --label-a broad --label-b scoped \
  --save-comparison comparison.json
mcp-doctor lab plan pilot.toml --save-plan scope-lab-output/plan.json
mcp-doctor lab run scope-lab-output/plan.json --max-attempts 12
mcp-doctor lab run scope-lab-output/plan.json --max-attempts 12 --resume
mcp-doctor lab report scope-lab-output/plan.json \
  --markdown scope-lab-output/report.md \
  --public-json scope-lab-output/public-report.json
```

`compare`, `plan`, and `report` do not make model or target-tool calls. `run`
invokes fixed `pnpm eval` arguments at concurrency 1, makes billable calls, and
writes ephemeral sandbox state. It records an immutable start and completion
event per attempt. A scored failure is complete. Missing results and process
failures remain separate from task failures. Resume verifies the plan, source
revisions, ledger chain, and artifact hashes, then skips validated completed
attempts. An untracked old result blocks execution.

For a three-task, one-repetition pilot there are 12 scheduled attempts. Review
the report and source artifacts before raising `repetitions` to five in a new
study. Use [the methodology template](lab-methodology-template.md) when sharing
results. The raw Evals artifacts remain local; the public JSON export checks for
paths, credentials, and transcript-shaped content.
