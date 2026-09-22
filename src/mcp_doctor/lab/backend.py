from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mcp_doctor.lab.models import LabError, LabPlan, PlannedAttempt


@dataclass(frozen=True)
class RunOutcome:
    argv: tuple[str, ...]
    result_path: Path
    returncode: int | None = None
    timed_out: bool = False
    error: str | None = None


class LabBackend(Protocol):
    def preflight(self, plan: LabPlan) -> None: ...
    def result_path(self, plan: LabPlan, attempt: PlannedAttempt) -> Path: ...
    def command_argv(self, plan: LabPlan, attempt: PlannedAttempt) -> tuple[str, ...]: ...
    def run_one(self, plan: LabPlan, attempt: PlannedAttempt, timeout: int) -> RunOutcome: ...
    def load_result(self, path: Path) -> bytes: ...


class SupabaseEvalsBackend:
    """Fixed-argv adapter for a pinned Supabase Evals checkout."""

    def preflight(self, plan: LabPlan) -> None:
        if shutil.which("pnpm") is None:
            raise LabError("pnpm is required to run the pinned Supabase Evals checkout.")
        if not Path(plan.contract_path).is_file():
            raise LabError("Generate scope-lab-contract.json in the pinned Evals checkout first.")
        if plan.run_index_base != 1:
            raise LabError("Supabase Evals uses 1-based run indexes; regenerate its contract.")

    def result_path(self, plan: LabPlan, attempt: PlannedAttempt) -> Path:
        checkout = Path(plan.manifest.sources.evals_checkout)
        run_index = attempt.repetition - 1 + plan.run_index_base
        return (
            checkout
            / "results"
            / attempt.experiment
            / attempt.task_id
            / f"run-{run_index}"
            / "result.json"
        )

    def command_argv(self, plan: LabPlan, attempt: PlannedAttempt) -> tuple[str, ...]:
        run_index = attempt.repetition - 1 + plan.run_index_base
        return (
            "pnpm",
            "eval",
            "--",
            "--experiment",
            attempt.experiment,
            "--eval",
            attempt.task_id,
            "--runs",
            "1",
            "--run-index",
            str(run_index),
            "--skip-existing",
            "--concurrency",
            "1",
        )

    def run_one(self, plan: LabPlan, attempt: PlannedAttempt, timeout: int) -> RunOutcome:
        checkout = Path(plan.manifest.sources.evals_checkout)
        result = self.result_path(plan, attempt)
        argv = self.command_argv(plan, attempt)
        try:
            process = subprocess.run(
                argv,
                cwd=checkout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RunOutcome(
                argv=argv, result_path=result, timed_out=True, error="attempt timeout"
            )
        except OSError as exc:
            return RunOutcome(argv=argv, result_path=result, error=f"process launch failed: {exc}")
        return RunOutcome(
            argv=argv,
            result_path=result,
            returncode=process.returncode,
            error=f"pnpm eval exited {process.returncode}" if process.returncode else None,
        )

    def load_result(self, path: Path) -> bytes:
        return path.read_bytes()


class FakeBackend:
    """Deterministic local backend; fixtures contain only synthetic data."""

    def __init__(
        self,
        fixtures: dict[tuple[str, str, int], bytes | str],
        *,
        failures: dict[tuple[str, str, int], str] | None = None,
    ):
        self.fixtures = fixtures
        self.failures = failures or {}
        self.calls: list[tuple[str, str, int]] = []

    def preflight(self, plan: LabPlan) -> None:
        return None

    def result_path(self, plan: LabPlan, attempt: PlannedAttempt) -> Path:
        return (
            Path(plan.output_dir)
            / "fake-results"
            / attempt.experiment
            / attempt.task_id
            / f"run-{attempt.repetition - 1 + plan.run_index_base}"
            / "result.json"
        )

    def command_argv(self, plan: LabPlan, attempt: PlannedAttempt) -> tuple[str, ...]:
        return (
            "fake-eval",
            attempt.experiment,
            attempt.task_id,
            str(attempt.repetition - 1 + plan.run_index_base),
        )

    def run_one(self, plan: LabPlan, attempt: PlannedAttempt, timeout: int) -> RunOutcome:
        key = (attempt.condition_id, attempt.task_id, attempt.repetition)
        self.calls.append(key)
        target = self.result_path(plan, attempt)
        argv = self.command_argv(plan, attempt)
        failure = self.failures.get(key)
        if failure == "interrupt":
            raise KeyboardInterrupt("injected interruption")
        if failure == "timeout":
            return RunOutcome(argv, target, timed_out=True, error="injected timeout")
        if failure == "process":
            return RunOutcome(argv, target, returncode=1, error="injected process failure")
        if failure == "missing":
            return RunOutcome(argv, target, returncode=0)
        data = self.fixtures.get(key)
        if data is None:
            return RunOutcome(argv, target, returncode=0)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        return RunOutcome(argv, target, returncode=0)

    def load_result(self, path: Path) -> bytes:
        return path.read_bytes()
