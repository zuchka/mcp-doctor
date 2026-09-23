import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp_doctor.diff import load_report, save_report
from mcp_doctor.lab.analysis import report_lab
from mcp_doctor.lab.backend import FakeBackend, SupabaseEvalsBackend
from mcp_doctor.lab.io import canonical, sha256, write_json
from mcp_doctor.lab.models import AttemptStatus, LabError, LabPlan, PlannedAttempt
from mcp_doctor.lab.planner import load_plan, plan_lab
from mcp_doctor.lab.report import public_export
from mcp_doctor.lab.runner import load_ledger, run_lab
from mcp_doctor.models import AnalysisReport, ServerInspection, ToolDefinition


def _setup(tmp_path: Path) -> tuple[Path, Path, dict]:
    checkout = tmp_path / "supabase-evals"
    checkout.mkdir()
    broad = tmp_path / "broad.json"
    scoped = tmp_path / "scoped.json"
    for path, names in ((broad, ["docs", "database", "account"]), (scoped, ["docs", "database"])):
        save_report(
            AnalysisReport(
                inspection=ServerInspection(
                    target="synthetic", tools=[ToolDefinition(name=name) for name in names]
                ),
                metrics={
                    "tool_count": len(names),
                    "total_definition_bytes": 10 * len(names),
                    "estimated_definition_tokens": 3 * len(names),
                },
                checks=[],
                policy={"version": 1},
            ),
            path,
        )
    fingerprints = {
        name: sha256(
            canonical(
                [
                    tool.model_dump(mode="json")
                    for tool in sorted(
                        load_report(path).inspection.tools, key=lambda item: item.name
                    )
                ]
            )
        )
        for name, path in (("broad", broad), ("scoped", scoped))
    }
    conditions = []
    experiments = []
    for surface, skills in (
        ("broad", "none"),
        ("scoped", "none"),
        ("broad", "supabase"),
        ("scoped", "supabase"),
    ):
        cid = f"{surface}-{skills}"
        features = (
            ["docs", "account", "database", "development", "debugging", "functions"]
            if surface == "broad"
            else ["docs", "database"]
        )
        skill_names = [] if skills == "none" else ["supabase", "supabase-postgres-best-practices"]
        conditions.append({"id": cid, "surface": surface, "skill_level": skills, "experiment": cid})
        experiments.append(
            {
                "id": cid,
                "agent": {
                    "harness": "codex",
                    "model": "synthetic-model",
                    "reasoning_effort": "medium",
                },
                "runtime": {"kind": "platform-lite", "ephemeral": True, "settings": {}},
                "features": features,
                "skills": skill_names,
                "mcp_server_version": "1.0.0",
                "tool_names": ["docs", "database", "account"]
                if surface == "broad"
                else ["docs", "database"],
                "tool_catalog_sha256": fingerprints[surface],
                "config_sha256": "c" * 64,
            }
        )
    contract = {
        "version": 1,
        "result_schema_version": 1,
        "run_index_base": 1,
        "evals_commit": "b" * 40,
        "skill_commit": "b" * 40,
        "tasks": [
            {
                "id": "task-one",
                "interface": "mcp",
                "runtime_supported": True,
                "skills_override": False,
            }
        ],
        "experiments": experiments,
    }
    write_json(checkout / "scope-lab-contract.json", contract)
    manifest = tmp_path / "lab.toml"
    sections = [
        'version = 1\nid = "synthetic"\noutput_dir = "./out"\n'
        'tasks = ["task-one"]\nrepetitions = 1\nseed = 42',
        '[sources]\nevals_checkout = "./supabase-evals"\n'
        f'evals_commit = "{"b" * 40}"\n'
        f'doctor_commit = "{"a" * 40}"\n'
        'doctor_version = "0.4.0"\nmcp_server_version = "1.0.0"',
        '[agent]\nharness = "codex"\nmodel = "synthetic-model"\nreasoning_effort = "medium"',
        '[runtime]\nkind = "platform-lite"',
        '[surfaces.broad]\nfeatures = ["docs", "account", "database", '
        '"development", "debugging", "functions"]\nreport = "./broad.json"',
        '[surfaces.scoped]\nfeatures = ["docs", "database"]\nreport = "./scoped.json"',
        "[skill_levels.none]\nskills = []",
        '[skill_levels.supabase]\nskills = ["supabase", "supabase-postgres-best-practices"]',
    ]
    sections.extend(
        f'[[conditions]]\nid = "{item["id"]}"\n'
        f'surface = "{item["surface"]}"\n'
        f'skill_level = "{item["skill_level"]}"\n'
        f'experiment = "{item["experiment"]}"'
        for item in conditions
    )
    sections.append("[limits]\nattempt_timeout_seconds = 30\nmaximum_attempts = 4")
    manifest.write_text("\n".join(sections) + "\n")
    plan_path = tmp_path / "out" / "plan.json"
    plan_lab(manifest, save_path=plan_path, check_revisions=False)
    return plan_path, manifest, contract


def _fixtures(contract: dict) -> dict[tuple[str, str, int], bytes]:
    results = {}
    for experiment in contract["experiments"]:
        cid = experiment["id"]
        passed = cid in {"scoped-none", "scoped-supabase"}
        result = {
            "experiment": cid,
            "eval": "task-one",
            "run": 1,
            "interface": "mcp",
            "experimentDisplay": {
                "modelId": "synthetic-model",
                "modelProvider": "openai",
                "agent": "codex",
                "reasoningEffort": "medium",
            },
            "scopeLab": {
                "version": 1,
                "configSha256": "c" * 64,
                "mcpServerVersion": "1.0.0",
                "features": experiment["features"],
                "skills": experiment["skills"],
                "runtime": {"kind": "platform-lite", "ephemeral": True, "settings": {}},
                "sandboxId": f"sandbox-{cid}",
            },
            "passed": passed,
            "checks": [{"name": "task_state", "passed": passed}],
            "skills": {"available": experiment["skills"], "loaded": []},
            "docs": {
                "calls": [
                    {
                        "source": "search_docs",
                        "query": "synthetic",
                        "pages": [],
                        "hasContent": True,
                        "resultChars": 25,
                    }
                ]
            },
            "stepCount": 3,
            "toolCallCount": 2,
            "agentRunDurationMs": 1000,
            "usage": [
                {
                    "model": "synthetic-model",
                    "inputTokens": 100,
                    "cacheReadInputTokens": 20,
                    "cacheWriteInputTokens": 10,
                    "outputTokens": 20,
                }
            ],
        }
        results[(cid, "task-one", 1)] = canonical(result)
    return results


def test_fake_end_to_end_and_resume(tmp_path: Path) -> None:
    path, _, contract = _setup(tmp_path)
    backend = FakeBackend(_fixtures(contract))
    events = run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    assert len(events) == 8
    assert len(backend.calls) == 4
    report = report_lab(path, check_revisions=False)
    assert report.difference_in_differences["complete_blocks"] == 1
    assert report.contrasts[0]["second_minus_first"] == 1
    assert sum(cell["passed"] for cell in report.cells) == 2
    assert report.cells[0]["tokens"]["input"]["median"] == 100
    assert report.cells[0]["tokens"]["cached_input"]["median"] == 30
    again = run_lab(path, max_attempts=4, resume=True, backend=backend, check_revisions=False)
    assert len(again) == 8
    assert len(backend.calls) == 4
    assert canonical(report) == canonical(report_lab(path, check_revisions=False))
    assert public_export(report)["study_id"] == "synthetic"


def test_missing_result_is_infrastructure_error(tmp_path: Path) -> None:
    path, _, contract = _setup(tmp_path)
    fixtures = _fixtures(contract)
    backend = FakeBackend(fixtures, failures={("broad-none", "task-one", 1): "missing"})
    run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    report = report_lab(path, check_revisions=False)
    broad = next(cell for cell in report.cells if cell["condition_id"] == "broad-none")
    assert broad["scored"] == 0
    assert broad["infrastructure_errors"] == 1
    assert broad["pass_rate"] is None


def test_untracked_result_blocks_paid_run(tmp_path: Path) -> None:
    path, _, contract = _setup(tmp_path)
    backend = FakeBackend(_fixtures(contract))
    plan = json.loads(path.read_text())
    first = plan["ordered_attempts"][0]
    result = backend.result_path(LabPlan.model_validate(plan), PlannedAttempt.model_validate(first))
    result.parent.mkdir(parents=True)
    result.write_text("{}")
    with pytest.raises(LabError, match="Untracked"):
        run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    assert backend.calls == []


def test_interrupt_resume_keeps_ledger_and_reports_missing_result(tmp_path: Path) -> None:
    path, _, contract = _setup(tmp_path)
    plan, digest = load_plan(path, check_revisions=False)
    first = plan.ordered_attempts[0]
    key = (first.condition_id, first.task_id, first.repetition)
    backend = FakeBackend(_fixtures(contract), failures={key: "interrupt"})
    with pytest.raises(KeyboardInterrupt):
        run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    assert [event.status for event in load_ledger(plan, digest)] == [AttemptStatus.STARTED]
    backend.failures.clear()
    run_lab(path, max_attempts=4, resume=True, backend=backend, check_revisions=False)
    report = report_lab(path, check_revisions=False)
    assert len(report.exclusions) == 1
    assert report.exclusions[0]["status"] == "missing_result"
    assert sum(cell["scored"] for cell in report.cells) == 3


@pytest.mark.parametrize(
    ("failure", "status"),
    [("process", "process_error"), ("timeout", "timeout"), ("missing", "missing_result")],
)
def test_infrastructure_failure_is_not_scored(tmp_path: Path, failure: str, status: str) -> None:
    path, _, contract = _setup(tmp_path)
    plan, _ = load_plan(path, check_revisions=False)
    first = plan.ordered_attempts[0]
    backend = FakeBackend(
        _fixtures(contract),
        failures={(first.condition_id, first.task_id, first.repetition): failure},
    )
    run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    report = report_lab(path, check_revisions=False)
    assert report.exclusions[0]["status"] == status
    assert sum(cell["scored"] for cell in report.cells) == 3


@pytest.mark.parametrize(
    "bad",
    [
        "corrupt",
        "wrong_model",
        "wrong_condition",
        "missing_passed",
        "wrong_sandbox",
        "wrong_features",
        "invalid_usage",
    ],
)
def test_bad_result_stops_before_next_paid_attempt(tmp_path: Path, bad: str) -> None:
    path, _, contract = _setup(tmp_path)
    plan, _ = load_plan(path, check_revisions=False)
    first = plan.ordered_attempts[0]
    key = (first.condition_id, first.task_id, first.repetition)
    fixtures = _fixtures(contract)
    if bad == "corrupt":
        fixtures[key] = b"{not json"
    else:
        result = json.loads(fixtures[key])
        if bad == "wrong_model":
            result["experimentDisplay"]["modelId"] = "wrong-model"
        elif bad == "wrong_condition":
            result["experiment"] = "other-condition"
        elif bad == "missing_passed":
            del result["passed"]
        elif bad == "wrong_features":
            result["scopeLab"]["features"] = [{"not": "a feature"}]
        elif bad == "invalid_usage":
            result["usage"][0]["cacheReadInputTokens"] = 200
        else:
            del result["scopeLab"]["sandboxId"]
        fixtures[key] = canonical(result)
    backend = FakeBackend(fixtures)
    with pytest.raises(LabError, match="provenance/schema"):
        run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    assert backend.calls == [key]


def test_changed_contract_or_result_hash_is_rejected(tmp_path: Path) -> None:
    path, _, contract = _setup(tmp_path)
    backend = FakeBackend(_fixtures(contract))
    contract_path = tmp_path / "supabase-evals" / "scope-lab-contract.json"
    original_contract = contract_path.read_bytes()
    contract_path.write_bytes(original_contract + b" ")
    with pytest.raises(LabError, match="contract changed"):
        run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    assert backend.calls == []
    contract_path.write_bytes(original_contract)
    run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    plan, digest = load_plan(path, check_revisions=False)
    event = next(event for event in load_ledger(plan, digest) if event.result_sha256)
    Path(event.result_path).write_text("{}")
    with pytest.raises(LabError, match="Raw result was changed"):
        load_ledger(plan, digest)


def test_importer_keeps_docs_queries_out_of_normalized_artifacts(tmp_path: Path) -> None:
    path, _, contract = _setup(tmp_path)
    backend = FakeBackend(_fixtures(contract))
    run_lab(path, max_attempts=4, backend=backend, check_revisions=False)
    plan, digest = load_plan(path, check_revisions=False)
    event = next(item for item in load_ledger(plan, digest) if item.attempt_path)
    normalized = json.loads(Path(event.attempt_path).read_text())
    assert normalized["docs_call_count"] == 1
    assert normalized["docs_call_metadata"]["source_search_docs"] == 1
    assert normalized["usage_by_model"]["synthetic-model"]["cached_input_tokens"] == 30
    assert normalized["usage_by_model"]["synthetic-model"]["reasoning_tokens"] is None
    assert "synthetic" not in json.dumps(normalized["docs_call_metadata"])
    report = report_lab(path, check_revisions=False)
    assert report.cells[0]["tokens"]["reasoning"] is None


def test_plan_rejects_zero_based_evals_contract(tmp_path: Path) -> None:
    _, manifest, contract = _setup(tmp_path)
    contract["run_index_base"] = 0
    write_json(tmp_path / "supabase-evals" / "scope-lab-contract.json", contract)
    with pytest.raises(LabError, match="run_index_base"):
        plan_lab(manifest, check_revisions=False)


def test_supabase_adapter_uses_fixed_argv_without_shell(tmp_path: Path, monkeypatch) -> None:
    path, _, _ = _setup(tmp_path)
    plan, _ = load_plan(path, check_revisions=False)
    attempt = plan.ordered_attempts[0]
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("mcp_doctor.lab.backend.subprocess.run", fake_run)
    outcome = SupabaseEvalsBackend().run_one(plan, attempt, timeout=30)
    assert outcome.returncode == 0
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[:3] == ("pnpm", "eval", "--")
    assert argv[argv.index("--experiment") + 1] == attempt.experiment
    assert argv[argv.index("--eval") + 1] == attempt.task_id
    assert argv[-2:] == ("--concurrency", "1")
    assert "shell" not in options


@pytest.mark.parametrize("change", ["task_skills", "experiment_features", "catalog"])
def test_plan_rejects_ineligible_or_mismatched_upstream_contract(
    tmp_path: Path, change: str
) -> None:
    _, manifest, contract = _setup(tmp_path)
    if change == "task_skills":
        contract["tasks"][0]["skills_override"] = True
    elif change == "experiment_features":
        contract["experiments"][0]["features"] = ["docs"]
    else:
        contract["experiments"][0]["tool_catalog_sha256"] = "0" * 64
    write_json(tmp_path / "supabase-evals" / "scope-lab-contract.json", contract)
    with pytest.raises(LabError):
        plan_lab(manifest, check_revisions=False)


def test_manifest_rejects_credentials_and_extra_keys(tmp_path: Path) -> None:
    _, manifest, _ = _setup(tmp_path)
    original = manifest.read_text()
    manifest.write_text(original + 'access_token = "secret"\n')
    with pytest.raises(LabError, match="Credentials"):
        plan_lab(manifest, check_revisions=False)
    manifest.write_text(original + 'arbitrary_command = "pnpm eval"\n')
    with pytest.raises(LabError, match="Invalid Lab manifest"):
        plan_lab(manifest, check_revisions=False)
