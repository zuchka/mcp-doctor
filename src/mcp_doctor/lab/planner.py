from __future__ import annotations

import random
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from mcp_doctor import __version__
from mcp_doctor.diff import load_report
from mcp_doctor.lab.io import canonical, load_json, load_toml, read_bytes, sha256, write_json
from mcp_doctor.lab.models import (
    EvalsContract,
    LabError,
    LabManifest,
    LabPlan,
    PlannedAttempt,
)
from mcp_doctor.surface_compare import compare_saved_surfaces

_SECRET_KEY = re.compile(r"(?i)(token|password|secret|credential|api[_-]?key|project[_-]?url)")
_URL = re.compile(r"(?i)https?://|postgres(?:ql)?://|sb_(?:secret|publishable)_")


def _no_secrets(value: Any, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(key):
                raise LabError(f"Credentials or project URLs are forbidden in {path}.{key}.")
            _no_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _no_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and _URL.search(value):
        raise LabError(f"URLs or access tokens are forbidden in {path}.")


def _git_head(path: Path, label: str) -> str:
    try:
        process = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LabError(f"Cannot verify {label} Git checkout {path}: {exc}") from exc
    return process.stdout.strip()


def _tracked_checkout_clean(path: Path, label: str) -> None:
    try:
        process = subprocess.run(
            ["git", "-C", str(path), "diff", "--quiet", "HEAD", "--"],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LabError(f"Cannot verify {label} worktree: {exc}") from exc
    if process.returncode != 0:
        raise LabError(f"{label} checkout has uncommitted tracked changes; pin a clean revision.")


def _catalog_fingerprint(report: Any) -> str:
    tools = [
        tool.model_dump(mode="json")
        for tool in sorted(report.inspection.tools, key=lambda item: item.name)
    ]
    return sha256(canonical(tools))


def verify_revisions(plan: LabPlan | LabManifest, *, doctor_root: Path | None = None) -> None:
    manifest = plan.manifest if isinstance(plan, LabPlan) else plan
    sources = manifest.sources
    root = doctor_root or Path(__file__).parents[3]
    expected = [
        (root, sources.doctor_commit, "Doctor"),
        (Path(sources.evals_checkout), sources.evals_commit, "Supabase Evals"),
    ]
    if sources.skill_checkout:
        expected.append((Path(sources.skill_checkout), sources.skill_commit or "", "skills"))
    for path, revision, label in expected:
        actual = _git_head(path, label)
        if actual != revision:
            raise LabError(f"{label} checkout is at {actual}, expected pinned {revision}.")
        _tracked_checkout_clean(path, label)
    if sources.doctor_version != __version__:
        raise LabError(f"Doctor version is {__version__}, expected {sources.doctor_version}.")


def _resolved_manifest(manifest: LabManifest, directory: Path) -> LabManifest:
    data = manifest.model_dump(mode="json")

    def resolve(value: str) -> str:
        return str((directory / value).expanduser().resolve())

    data["output_dir"] = resolve(data["output_dir"])
    data["sources"]["evals_checkout"] = resolve(data["sources"]["evals_checkout"])
    if data["sources"]["skill_checkout"]:
        data["sources"]["skill_checkout"] = resolve(data["sources"]["skill_checkout"])
    for surface in data["surfaces"].values():
        surface["report"] = resolve(surface["report"])
    return LabManifest.model_validate(data)


def _validate_contract(manifest: LabManifest, contract: EvalsContract) -> None:
    if contract.evals_commit != manifest.sources.evals_commit:
        raise LabError("Experiment contract Evals commit differs from the manifest pin.")
    expected_skill_commit = manifest.sources.skill_commit or manifest.sources.evals_commit
    if contract.skill_commit != expected_skill_commit:
        raise LabError("Experiment contract skill revision differs from the manifest pin.")
    tasks = {task.id: task for task in contract.tasks}
    if len(tasks) != len(contract.tasks):
        raise LabError("Experiment contract contains duplicate task IDs.")
    for task_id in manifest.tasks:
        task = tasks.get(task_id)
        if task is None:
            raise LabError(f"Task {task_id!r} is absent from the pinned experiment contract.")
        if task.interface != "mcp" or not task.runtime_supported or task.skills_override:
            raise LabError(
                f"Task {task_id!r} is not an eligible MCP task in platform-lite "
                "or overrides skills."
            )
    experiments = {item.id: item for item in contract.experiments}
    if len(experiments) != len(contract.experiments):
        raise LabError("Experiment contract contains duplicate experiment IDs.")
    for condition in manifest.conditions:
        item = experiments.get(condition.experiment)
        if item is None:
            raise LabError(
                f"Experiment {condition.experiment!r} is absent from the pinned contract. "
                "Generate scope-lab-contract.json in the Evals checkout."
            )
        if item.agent != manifest.agent or item.runtime != manifest.runtime:
            raise LabError(f"Experiment {item.id!r} changes agent or runtime settings.")
        if set(item.features) != set(manifest.surfaces[condition.surface].features):
            raise LabError(f"Experiment {item.id!r} has incorrect MCP features.")
        if set(item.skills) != set(manifest.skill_levels[condition.skill_level].skills):
            raise LabError(f"Experiment {item.id!r} has incorrect preinstalled skills.")
        if item.mcp_server_version != manifest.sources.mcp_server_version:
            raise LabError(f"Experiment {item.id!r} has a different MCP server version.")
        report = load_report(Path(manifest.surfaces[condition.surface].report))
        if "tools" in report.inspection.listing_errors:
            raise LabError(f"Static report for {condition.surface} has a tool-listing error.")
        if set(item.tool_names) != {tool.name for tool in report.inspection.tools}:
            raise LabError(
                f"Static catalog for {condition.surface} does not match experiment {item.id!r}."
            )
        if item.tool_catalog_sha256 != _catalog_fingerprint(report):
            raise LabError(
                f"Static tool definitions for {condition.surface} differ "
                f"from experiment {item.id!r}."
            )


def plan_lab(
    manifest_path: Path, *, save_path: Path | None = None, check_revisions: bool = True
) -> LabPlan:
    manifest_path = manifest_path.expanduser().resolve()
    raw = read_bytes(manifest_path)
    try:
        _no_secrets(tomllib.loads(raw.decode()))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LabError(f"Invalid manifest {manifest_path}: {exc}") from exc
    manifest = _resolved_manifest(load_toml(manifest_path, LabManifest), manifest_path.parent)
    if check_revisions:
        verify_revisions(manifest)
    contract_path = Path(manifest.sources.evals_checkout) / "scope-lab-contract.json"
    contract = load_json(contract_path, EvalsContract)
    _validate_contract(manifest, contract)
    reports = {name: Path(surface.report) for name, surface in manifest.surfaces.items()}
    comparison = compare_saved_surfaces(
        reports["broad"], reports["scoped"], label_a="broad", label_b="scoped"
    )
    if not comparison.valid_for_lab:
        raise LabError("Static surface comparison is invalid because a tool listing failed.")
    rng = random.Random(manifest.seed)
    order: list[PlannedAttempt] = []
    conditions = list(manifest.conditions)
    for repetition in range(1, manifest.repetitions + 1):
        for task_id in manifest.tasks:
            block = conditions.copy()
            rng.shuffle(block)
            for condition in block:
                order.append(
                    PlannedAttempt(
                        sequence=len(order),
                        task_id=task_id,
                        repetition=repetition,
                        condition_id=condition.id,
                        experiment=condition.experiment,
                    )
                )
    plan = LabPlan(
        manifest_path=str(manifest_path),
        manifest_sha256=sha256(raw),
        contract_path=str(contract_path),
        contract_sha256=sha256(read_bytes(contract_path)),
        run_index_base=contract.run_index_base,
        manifest=manifest,
        selected_tasks=tuple(
            next(task for task in contract.tasks if task.id == task_id)
            for task_id in manifest.tasks
        ),
        experiment_settings={
            condition.id: next(
                item for item in contract.experiments if item.id == condition.experiment
            )
            for condition in manifest.conditions
        },
        report_sha256={name: sha256(read_bytes(path)) for name, path in reports.items()},
        surface_fingerprints={
            name: _catalog_fingerprint(load_report(path)) for name, path in reports.items()
        },
        surface_comparison_sha256=sha256(canonical(comparison)),
        comparability_warnings=tuple(item.code for item in comparison.warnings),
        ordered_attempts=tuple(order),
        output_dir=manifest.output_dir,
    )
    if save_path is not None:
        write_json(save_path.expanduser().resolve(), plan, exclusive=True)
    return plan


def load_plan(path: Path, *, check_revisions: bool = True) -> tuple[LabPlan, str]:
    path = path.expanduser().resolve()
    raw = read_bytes(path)
    plan = load_json(path, LabPlan)
    if check_revisions:
        verify_revisions(plan)
    if sha256(read_bytes(Path(plan.manifest_path))) != plan.manifest_sha256:
        raise LabError("Lab manifest changed since planning.")
    if sha256(read_bytes(Path(plan.contract_path))) != plan.contract_sha256:
        raise LabError("Experiment contract changed since planning.")
    for name, expected in plan.report_sha256.items():
        report_path = Path(plan.manifest.surfaces[name].report)
        if sha256(read_bytes(report_path)) != expected:
            raise LabError(f"Static {name} report changed since planning.")
    regenerated = plan_lab(Path(plan.manifest_path), check_revisions=False)
    if canonical(regenerated) != canonical(plan):
        raise LabError("Saved Lab plan differs from validated manifest and experiment contract.")
    return plan, sha256(raw)
