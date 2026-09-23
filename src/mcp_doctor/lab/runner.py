"""Sequential Lab execution and durable, append-only ledger."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from mcp_doctor.lab.backend import LabBackend, RunOutcome, SupabaseEvalsBackend
from mcp_doctor.lab.importer import ResultError, import_result
from mcp_doctor.lab.io import canonical, load_json, read_bytes, sha256, write_json
from mcp_doctor.lab.models import (
    AttemptStatus,
    EvalsContract,
    LabAttempt,
    LabError,
    LabPlan,
    LedgerEvent,
    PlannedAttempt,
)
from mcp_doctor.lab.planner import load_plan


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key(item: LedgerEvent | PlannedAttempt) -> tuple[str, int, str]:
    return item.task_id, item.repetition, item.condition_id


def _ledger_dir(plan: LabPlan) -> Path:
    return Path(plan.output_dir) / "ledger"


def load_ledger(plan: LabPlan, plan_sha256: str) -> list[LedgerEvent]:
    directory = _ledger_dir(plan)
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    events: list[LedgerEvent] = []
    previous: str | None = None
    pending: LedgerEvent | None = None
    for index, path in enumerate(paths, start=1):
        if path.name != f"{index:08d}.json":
            raise LabError(f"Ledger event numbering is not contiguous at {path}.")
        raw = read_bytes(path)
        event = load_json(path, LedgerEvent)
        if event.sequence != index or event.previous_sha256 != previous:
            raise LabError(f"Ledger hash chain is invalid at {path}.")
        if event.plan_sha256 != plan_sha256 or event.study_id != plan.manifest.id:
            raise LabError(f"Ledger event {path} belongs to a different plan.")
        if event.status == AttemptStatus.STARTED:
            if pending is not None:
                raise LabError(
                    f"Ledger starts a second attempt before finishing {pending.sequence}."
                )
            pending = event
        else:
            if (
                pending is None
                or _key(pending) != _key(event)
                or pending.invocation != event.invocation
            ):
                raise LabError(f"Ledger completion {path} has no matching start event.")
            if not event.attempt_path or not event.attempt_sha256:
                raise LabError(f"Completed ledger event {path} lacks an attempt artifact.")
            if sha256(read_bytes(Path(event.attempt_path))) != event.attempt_sha256:
                raise LabError(f"Normalized attempt was changed after {path}.")
            normalized = load_json(Path(event.attempt_path), LabAttempt)
            if (
                normalized.status != event.status
                or normalized.task_id != event.task_id
                or normalized.repetition != event.repetition
                or normalized.condition_id != event.condition_id
            ):
                raise LabError(f"Ledger completion {path} disagrees with its attempt artifact.")
            if (
                event.result_path
                and event.result_sha256
                and sha256(read_bytes(Path(event.result_path))) != event.result_sha256
            ):
                raise LabError(f"Raw result was changed after {path}.")
            pending = None
        previous = sha256(raw)
        events.append(event)
    return events


def _append(
    plan: LabPlan, plan_sha256: str, events: list[LedgerEvent], **fields: object
) -> LedgerEvent:
    previous = sha256(canonical(events[-1])) if events else None
    event = LedgerEvent(
        sequence=len(events) + 1,
        plan_sha256=plan_sha256,
        study_id=plan.manifest.id,
        previous_sha256=previous,
        **fields,
    )
    path = _ledger_dir(plan) / f"{event.sequence:08d}.json"
    write_json(path, event, exclusive=True)
    events.append(event)
    return event


@contextmanager
def _exclusive_run(plan: LabPlan) -> Iterator[None]:
    root = Path(plan.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".run.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LabError("Another Lab runner holds the study lock.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _error_attempt(
    plan: LabPlan, item: PlannedAttempt, status: AttemptStatus, reason: str
) -> LabAttempt:
    return LabAttempt(
        study_id=plan.manifest.id,
        task_id=item.task_id,
        repetition=item.repetition,
        condition_id=item.condition_id,
        experiment=item.experiment,
        status=status,
        passed=None,
        error_category=status.value,
        error_detail=reason,
    )


def _consume(
    plan: LabPlan,
    item: PlannedAttempt,
    outcome: RunOutcome | None,
    backend: LabBackend,
    contract: EvalsContract,
) -> tuple[LabAttempt, str | None, str | None]:
    path = backend.result_path(plan, item)
    raw: bytes | None = None
    if path.is_file():
        with suppress(OSError):
            raw = backend.load_result(path)
    source_path = str(path) if raw is not None else None
    source_hash = sha256(raw) if raw is not None else None
    if outcome and outcome.timed_out:
        return (
            _error_attempt(plan, item, AttemptStatus.TIMEOUT, outcome.error or "timeout"),
            source_path,
            source_hash,
        )
    if outcome and (outcome.returncode is None or outcome.returncode != 0):
        return (
            _error_attempt(
                plan, item, AttemptStatus.PROCESS_ERROR, outcome.error or "process failure"
            ),
            source_path,
            source_hash,
        )
    if raw is None:
        return (
            _error_attempt(plan, item, AttemptStatus.MISSING_RESULT, "result.json is missing"),
            None,
            None,
        )
    try:
        attempt = import_result(raw, path=path, plan=plan, attempt=item, contract=contract)
    except ResultError as exc:
        attempt = _error_attempt(plan, item, exc.category, str(exc))
    return attempt, source_path, source_hash


def _finish(
    plan: LabPlan,
    plan_sha256: str,
    events: list[LedgerEvent],
    start: LedgerEvent,
    item: PlannedAttempt,
    outcome: RunOutcome | None,
    backend: LabBackend,
    contract: EvalsContract,
) -> LabAttempt:
    attempt, result_path, result_hash = _consume(plan, item, outcome, backend, contract)
    if result_path and result_hash and attempt.raw_result_path is None:
        for root in (Path(plan.manifest.sources.evals_checkout), Path(plan.output_dir)):
            try:
                relative = str(Path(result_path).resolve().relative_to(root.resolve()))
                attempt = attempt.model_copy(
                    update={"raw_result_path": relative, "raw_result_sha256": result_hash}
                )
                break
            except ValueError:
                continue
    if attempt.sandbox_id:
        for prior in events:
            if prior.attempt_path and prior.status in (AttemptStatus.PASSED, AttemptStatus.FAILED):
                previous_attempt = load_json(Path(prior.attempt_path), LabAttempt)
                if previous_attempt.sandbox_id == attempt.sandbox_id:
                    attempt = _error_attempt(
                        plan,
                        item,
                        AttemptStatus.PROVENANCE_MISMATCH,
                        "Ephemeral sandbox ID was reused by another attempt.",
                    )
                    break
    artifact = (
        Path(plan.output_dir) / "attempts" / f"{item.sequence:05d}-{start.invocation:02d}.json"
    )
    data = canonical(attempt)
    if artifact.exists():
        if read_bytes(artifact) != data:
            raise LabError(f"Untracked or changed normalized artifact at {artifact}.")
        digest = sha256(data)
    else:
        digest = write_json(artifact, attempt, exclusive=True)
    _append(
        plan,
        plan_sha256,
        events,
        task_id=item.task_id,
        repetition=item.repetition,
        condition_id=item.condition_id,
        invocation=start.invocation,
        started_at=start.started_at,
        ended_at=_now(),
        status=attempt.status,
        command_argv=(outcome.argv if outcome and outcome.argv else start.command_argv),
        result_path=result_path,
        result_sha256=result_hash,
        attempt_path=str(artifact),
        attempt_sha256=digest,
        error_category=attempt.error_category,
    )
    return attempt


def run_lab(
    plan_path: Path,
    *,
    max_attempts: int,
    resume: bool = False,
    backend: LabBackend | None = None,
    check_revisions: bool = True,
) -> list[LedgerEvent]:
    plan, plan_hash = load_plan(plan_path, check_revisions=check_revisions)
    if max_attempts != plan.manifest.limits.maximum_attempts:
        raise LabError(
            f"Confirm the configured maximum of "
            f"{plan.manifest.limits.maximum_attempts} attempts with --max-attempts."
        )
    adapter = backend or SupabaseEvalsBackend()
    adapter.preflight(plan)
    contract = load_json(Path(plan.contract_path), EvalsContract)
    with _exclusive_run(plan):
        events = load_ledger(plan, plan_hash)
        if events and not resume:
            raise LabError("This study has a ledger; use --resume to continue it.")
        planned_keys = {_key(item) for item in plan.ordered_attempts}
        if any(_key(event) not in planned_keys for event in events):
            raise LabError("Ledger contains an attempt outside the plan.")
        for item in plan.ordered_attempts:
            history = [event for event in events if _key(event) == _key(item)]
            if history and history[-1].status == AttemptStatus.STARTED:
                start = history[-1]
                recovered = _finish(plan, plan_hash, events, start, item, None, adapter, contract)
                if recovered.status in (
                    AttemptStatus.PROVENANCE_MISMATCH,
                    AttemptStatus.SCHEMA_ERROR,
                ):
                    raise LabError(
                        f"Recovered result for {_key(item)} failed provenance/schema validation."
                    )
                history = [event for event in events if _key(event) == _key(item)]
            if history and history[-1].status in (AttemptStatus.PASSED, AttemptStatus.FAILED):
                continue
            invocation = 1 + sum(event.status == AttemptStatus.STARTED for event in history)
            if history and invocation > 1 + plan.manifest.limits.infrastructure_retries:
                continue
            if history and history[-1].status in (
                AttemptStatus.PROVENANCE_MISMATCH,
                AttemptStatus.SCHEMA_ERROR,
                AttemptStatus.CORRUPT_RESULT,
            ):
                raise LabError(
                    f"Attempt {_key(item)} needs a compatible, verified result before continuing."
                )
            if (
                len([event for event in events if event.status == AttemptStatus.STARTED])
                >= max_attempts
            ):
                raise LabError("Configured maximum attempt count reached.")
            result_path = adapter.result_path(plan, item)
            if result_path.exists():
                raise LabError(
                    f"Untracked or previous result exists at {result_path}; "
                    "refusing --skip-existing."
                )
            start = _append(
                plan,
                plan_hash,
                events,
                task_id=item.task_id,
                repetition=item.repetition,
                condition_id=item.condition_id,
                invocation=invocation,
                started_at=_now(),
                status=AttemptStatus.STARTED,
                command_argv=adapter.command_argv(plan, item),
                result_path=str(result_path),
            )
            try:
                outcome = adapter.run_one(plan, item, plan.manifest.limits.attempt_timeout_seconds)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                outcome = RunOutcome(
                    argv=(), result_path=result_path, error=f"{type(exc).__name__}: {exc}"
                )
            attempt = _finish(plan, plan_hash, events, start, item, outcome, adapter, contract)
            if attempt.status in (
                AttemptStatus.PROVENANCE_MISMATCH,
                AttemptStatus.SCHEMA_ERROR,
                AttemptStatus.CORRUPT_RESULT,
            ):
                raise LabError(
                    f"Attempt {_key(item)} failed provenance/schema validation: "
                    f"{attempt.error_detail}"
                )
        return events
