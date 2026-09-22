# MCP Doctor extension spec: Scope Lab

Status: Doctor-side implementation complete; companion Evals configuration and pilot pending.
Target: the first release after MCP Doctor v0.4.
Owner: MCP Doctor project. Companion Supabase experiment definitions live in a pinned
Supabase Evals checkout, not in Doctor's core package.

## 1. Purpose and boundary

Support a reproducible experiment on whether a narrower MCP tool surface helps an
agent complete real tasks. The first study is a 2×2 Supabase experiment:

| Condition | MCP features | Preinstalled skills |
| --- | --- | --- |
| broad / no skills | `docs,account,database,development,debugging,functions` | none |
| scoped / no skills | `docs,database` | none |
| broad / skills | broad set above | `supabase`, `supabase-postgres-best-practices` |
| scoped / skills | scoped set above | same two skills |

This is an experiment in **tool-surface scope**, not a claim that a scoped surface is
universally better. Supabase Evals owns task execution and task-state scoring. Doctor owns
static surface inspection, study planning, orchestration, artifact validation, aggregation,
and presentation. Do not replace Supabase Evals' scorers with Doctor's v0.3 graders.

MCP Doctor v0.4 already has `inspect`, directional `diff`, native representative-task
`eval`, compatible-revision `eval-diff`, and five MCP tools. Reuse their report loading,
inspection, and CLI/MCP workflow patterns. Do **not** change the meaning or compatibility
rules of `diff`, `eval-diff`, `compare_inspection_reports`, or `compare_eval_runs`.

## 2. Deliverables

1. A symmetric static `compare` command/API for two intentional peer surfaces.
2. A strict, versioned Lab manifest and non-billable plan/preflight.
3. A backend interface with a deterministic fake backend and a Supabase Evals backend.
4. A strict importer for Supabase Evals `result.json` artifacts.
5. Sequential, seeded, resumable 2×2 execution with an immutable run ledger.
6. Per-task and pooled summaries, prespecified contrasts, and a Markdown report.
7. MCP tools for comparison, planning, running, and reporting, using the same workflows
   as the CLI and the existing local-STDIO security boundary.
8. Documentation and fixtures that let another person reproduce the study from pinned
   revisions without access to private credentials or production Supabase projects.

## 3. Symmetric static surface comparison

Add `mcp-doctor compare A.json B.json [--label-a broad] [--label-b scoped]
[--json] [--save-comparison PATH]`. Inputs are saved v0.4 `AnalysisReport` format-1
artifacts. This command is read-only and never reconnects or calls server tools.

Return a new versioned `SurfaceComparison` artifact; never reuse the directional
`ReportDiff` schema. Include:

- Labels, target/server identity, report SHA-256 hashes, analysis and policy versions.
- Tool names unique to A, unique to B, and shared.
- For shared names, any changed `title`, `description`, `input_schema`,
  `output_schema`, or `annotations`, with field names and before/after fingerprints.
  Canonicalize JSON object keys, retain array order, and do not normalize away prose.
  Compare `meta` separately; do not silently treat volatile metadata as a schema change.
- Side-by-side tool count, definition bytes/tokens, schema complexity, finding counts,
  and every shared numeric metric, plus an explicitly named `B − A` delta.
- Findings present only on A, only on B, and on both, keyed by existing finding
  fingerprint. Show suppression status on each side. Use neutral language, not
  `new`, `resolved`, `regression`, or a CI pass/fail verdict.
- Machine-readable comparability warnings for different Doctor analysis versions,
  policies, or missing metrics. If either tool listing has an error, fail closed: do
  not claim a tool is absent, and mark the comparison invalid for Lab preflight.

Swapping A and B must swap the unique sets and finding sides, preserve shared sets and
field-change identities, and negate numeric deltas. Preserve input paths only as
provenance; comparisons must not depend on paths or report creation time. CLI exit 0
means a valid comparison even if the surfaces differ; exit 2 means invalid input.

Add `compare_mcp_surfaces(report_a_path, report_b_path, label_a?, label_b?)` to the
MCP server. It accepts absolute local paths, is read-only, returns bounded summaries
and the full artifact path when requested, and follows the existing 25-item/omitted-count
response convention. Do not overload `compare_inspection_reports`.

## 4. Lab manifest and plan

Use a strict version-1 TOML or JSON manifest (`extra=forbid`) with these required
concepts:

- Study ID and output directory; pinned Doctor version, Supabase Evals Git commit,
  MCP server package version or local build commit, skill source revision, model ID,
  reasoning effort, harness, and runtime settings.
- Exactly two surface levels and two skill levels for this initial 2×2 workflow;
  four explicitly named Supabase Evals experiment IDs mapped one-to-one to the cells.
- Ordered task IDs; expected task `interface: mcp`; repetition target; seed; per-attempt
  timeout; maximum attempt count/cost guard; paths to A/B static reports.
- An explicit sandbox mode. For this study, require platform-lite/ephemeral state and
  refuse a production project URL or access token in the manifest. Credentials, if
  needed by a backend, come from environment/secret management, never saved to plans.

Suggested concrete TOML shape (placeholder revisions must be replaced before use):

```toml
version = 1
id = "supabase-scope-pilot"
output_dir = "./scope-lab-output"
tasks = [
  "build-rls-003-org-roles-permissions",
  "resolve-security-002-rls-cross-tenant-leak",
  "resolve-dataapi-001-empty-results",
]
repetitions = 1
seed = 41827

[sources]
evals_checkout = "./supabase-evals"
evals_commit = "<40-character-commit>"
doctor_commit = "<40-character-commit>"
mcp_server_version = "<exact-version>"

[agent]
harness = "codex"
model = "gpt-5.6-sol"
reasoning_effort = "medium"

[runtime]
kind = "platform-lite"

[surfaces.broad]
features = ["docs", "account", "database", "development", "debugging", "functions"]
report = "./reports/broad.json"

[surfaces.scoped]
features = ["docs", "database"]
report = "./reports/scoped.json"

[skill_levels.none]
skills = []

[skill_levels.supabase]
skills = ["supabase", "supabase-postgres-best-practices"]

[[conditions]]
id = "broad-no-skills"
surface = "broad"
skill_level = "none"
experiment = "<exact-experiment-id>"

[[conditions]]
id = "scoped-no-skills"
surface = "scoped"
skill_level = "none"
experiment = "<exact-experiment-id>"

[[conditions]]
id = "broad-skills"
surface = "broad"
skill_level = "supabase"
experiment = "<exact-experiment-id>"

[[conditions]]
id = "scoped-skills"
surface = "scoped"
skill_level = "supabase"
experiment = "<exact-experiment-id>"

[limits]
attempt_timeout_seconds = 720
maximum_attempts = 60
```

Resolve relative paths against the manifest's directory and record their absolute,
resolved form in the plan. A skill source in the Evals checkout inherits
`evals_commit`; an external skill source needs its own commit field. The manifest
declares intent, while preflight verifies the four actual experiment configurations.

`mcp-doctor lab plan MANIFEST [--json] [--save-plan PATH]` must validate and resolve
the full attempt matrix **without running an agent or target tools**. The plan records
manifest hash, all pinned revisions, selected tasks, actual static surface fingerprints,
expected condition settings, number of attempts, and the deterministic execution order.
Static inspections may be supplied as saved reports; live inspection is an explicit
separate step and must be identified as such. Planning must not mutate Evals results.

Preflight checks:

- All four experiment IDs and all task IDs exist in the pinned checkout. Use Supabase
  Evals' own `--dry`/discovery mode where possible, but do not treat human-readable
  `PLAN` lines as proof of feature or skill configuration.
- Each selected task declares `interface: mcp`, does not override `skills`, and can
  run in the chosen runtime. Reject a skipped/unsupported cell before paid execution.
- All four cells use the same agent/model/reasoning/harness settings except the two
  declared factors. Verify these from a machine-readable experiment contract or
  explicit companion metadata generated in the Evals checkout; merely trusting the
  manifest is insufficient. If upstream cannot expose that contract, preflight must
  stop with a precise action item rather than guessing from TypeScript text.
- Actual static catalogs match the declared broad/scoped feature expectations and
  have no listing errors. A static report cannot prove an experiment launched with
  those features, so runtime provenance must be checked separately as well.
- Report analysis policy/version match for comparable diagnostic metrics. Tool-set
  comparison remains meaningful if they differ, but metrics/findings are flagged.
- Detect duplicate condition IDs, repeated task IDs, stale or unpinned revisions,
  missing report files, unsupported result schema, and total attempts above the cap.

Do not require a Doctor v0.3 capability map for Supabase Evals tasks; its own
scenario/scorer contract is authoritative here.

## 5. Backend, importer, and result contract

Define a small internal `LabBackend` protocol: `preflight(plan)`,
`run_one(condition, task_id, repetition, timeout)`, and `load_result(path)`.
Ship a fake backend with canned result fixtures and injectable failures. The Supabase
backend invokes fixed `pnpm eval` argv with validated experiment/task IDs, `--runs N`,
`--run-index K`, `--skip-existing`, and concurrency 1; never use a shell or an arbitrary
command template from the manifest. Pin and record the checkout Git commit. The known
artifact layout is `results/<experiment>/<eval>/run-<n>/result.json`.

Importer requirements for each artifact:

- Validate expected `experiment`, `eval`, `run` (when present), `interface`, and
  `experimentDisplay` model/harness metadata. Require `passed: boolean` for a scored
  result; never turn missing `passed` into `false`.
- Preserve named `checks`, `skills.available` and `skills.loaded`, docs-call metadata,
  `stepCount`, `toolCallCount`, `agentRunDurationMs`, and usage per model. Cached
  input tokens are subsets of input tokens; never add them to total input again.
- Store the raw result's SHA-256 and relative path. Normalize only fields needed for
  analysis into a versioned Doctor `LabAttempt`; keep a pointer to the raw artifact.
  Unknown upstream fields may be retained in the raw file but not silently promoted.
- Check skills exposure against the condition: no-skills must have no available
  Supabase skills; skills cells must have the declared two available. `loaded` is an
  observed behavior, **not** a requirement to pass or a substitute for `available`.
- Classify process failure, timeout, missing/corrupt result, provenance mismatch,
  scored task failure, and scored pass separately. Missing results are infrastructure
  failures, never task failures. If an upstream version changes the schema, fail with
  an actionable compatibility error.

No task prompts, tool arguments/results, access tokens, or full transcripts are copied
into Doctor's public summary by default. Raw upstream artifacts stay local and are
referenced by path/hash. Provide a redaction/public-export check before publication.

## 6. Execution, resume, and analysis

`mcp-doctor lab run PLAN [--resume] [--max-attempts N]` is the only billable/stateful
step. It must require explicit confirmation/flag for the configured maximum attempts
and show the model, task count, and estimated upper bound first. Execute sequentially.
For each repetition and task, create a four-cell block and shuffle its order using the
saved seed; record the resulting complete order in the plan. This avoids always giving
one condition a first/last-position advantage. Each attempt gets fresh Evals state.

Write an append-only ledger atomically after each attempt with a stable key
`(study_id, task_id, repetition, condition_id)`, start/end timestamps, status,
command arguments with secrets removed, source result path/hash, and error category.
On `--resume`, verify plan hash, source revisions, and prior artifact hashes; skip only
validated completed attempts. Never overwrite a different successful artifact or
silently adopt an untracked old `result.json`. A scored `passed=false` is completed;
an infrastructure failure is retryable only by explicit retry policy. Ensure
interruption leaves the study recoverable. The fake backend must exercise all of this.

`mcp-doctor lab report PLAN [--json] [--markdown PATH]` reads the ledger and results;
it never executes tasks. Report, per cell and per task: scheduled, scored, passed,
failed, infrastructure errors, pass rate with denominator, check pass counts, median
tool calls, median steps, median duration, and token-use distribution where present.
Show docs usage and skill loading as descriptive secondary measures, not as success.
Use `N/A` for missing metrics rather than zero.

Prespecify four contrasts, with scoped minus broad calculated separately within each
skill level, and skills minus no-skills separately within each scope level. Also show
the difference-in-differences as descriptive. Pair by task/repetition block, show
the number of complete paired blocks, and do not silently pool incomparable runs.
Report raw counts and uncertainty appropriate for small samples; do not claim
statistical significance from a three-task/one-run pilot. The report must carry
configuration, hashes, exclusions, limitations, and the exact definition of every
metric. Missing/error attempts are excluded from scored pass-rate denominators but
reported prominently; also show an all-scheduled sensitivity view.

## 7. MCP interface

Add `preflight_mcp_lab(manifest_path, save_plan_path?)`,
`run_mcp_lab(plan_path, max_attempts, resume=false)`, and
`report_mcp_lab(plan_path, markdown_path?)`. Absolute paths only, matching v0.4.
`run_mcp_lab` is a native background task and explicitly advertises billable model
calls and sandbox writes. Plan/compare/report return bounded summaries and artifact
paths. Existing five MCP tools remain unchanged. The CLI and MCP tools call shared
workflows, not separate implementations.

The tool description must distinguish Doctor's native `run_mcp_eval` (custom suite and
deterministic Doctor graders) from `run_mcp_lab` (external Supabase Evals scorer).
No lab tool accepts an arbitrary executable or credentials as input.

## 8. Implementation sequence and acceptance criteria

Suggested slices (each independently testable):

1. Symmetric `SurfaceComparison` model, pure comparison function, CLI and MCP tool.
2. Strict Lab manifest, plan schema, and static/external preflight.
3. Fake backend and deterministic end-to-end tests, including resume and failures.
4. Supabase Evals adapter/importer and machine-readable experiment provenance contract.
5. Seeded blocked runner, ledger, aggregation, Markdown/JSON reporting.
6. MCP Lab tools, examples, README, and public methodology template.

Done means:

- Existing v0.4 CLI/MCP behavior and tests still pass; no report/run format-1 breakage.
- A/B swap invariants and changed shared-tool definitions are covered by tests.
- Plan/compare/report perform zero model calls and zero target tool calls.
- Four-cell matrix, pinned settings, task eligibility, skill overrides, and actual
  feature catalogs are validated before a paid run.
- Fixtures test pass, scored fail, missing/corrupt result, process failure, wrong
  condition/model, stale revision, interrupt/resume, and duplicate artifacts.
- Fake backend produces the same report bytes for a fixed seed and fixtures.
- A one-run three-task pilot can be completed and honestly reported; five runs per
  task are a separate expansion gated on pilot quality, not needed to call the
  implementation complete.

## 9. Pilot recipe and ownership

The initial tasks are candidates, not a promise of compatibility:
`build-rls-003-org-roles-permissions`,
`resolve-security-002-rls-cross-tenant-leak`, and
`resolve-dataapi-001-empty-results`. They currently declare `interface: mcp` in the
inspected Supabase Evals checkout. Confirm they still exist and are supported at the
pinned commit, then run exactly once per condition before expanding repetitions.

Companion work in Supabase Evals: create four explicit experiment definitions using
the same agent/runtime settings, with only `supabaseMcpServer({features: ...})` and
`skills` changed; expose machine-readable resolved configuration/provenance if the
current runner cannot. Doctor should not edit upstream experiment definitions at
runtime. The current Supabase helper's default feature list is the broad set above;
the feature list and result schema are upstream dependencies and must be pinned.

Useful local extension points: `src/mcp_doctor/models.py`, `diff.py`, `workflows.py`,
`cli.py`, `server.py`, and a new `src/mcp_doctor/lab/` package. Keep the Supabase
adapter isolated from the generic comparison and Lab models. Avoid putting fixture
or pilot output into the package source tree.
