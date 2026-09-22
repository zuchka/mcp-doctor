from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from pydantic import ValidationError

from mcp_doctor.evals.models import EvalRun


class EvalRunFileError(ValueError):
    """Raised when a saved eval run cannot be read, validated, or written."""


def save_eval_run(run: EvalRun, path: Path) -> None:
    resolved = path.expanduser().resolve()
    payload = json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    temporary_path: str | None = None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=resolved.parent, delete=False
        ) as stream:
            temporary_path = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, resolved)
    except OSError as exc:
        if temporary_path:
            with suppress(OSError):
                Path(temporary_path).unlink(missing_ok=True)
        raise EvalRunFileError(f"Could not write eval run {resolved}: {exc}") from exc


def load_eval_run(path: Path) -> EvalRun:
    resolved = path.expanduser().resolve()
    try:
        payload = json.loads(resolved.read_text())
        if not isinstance(payload, dict) or "eval_run_format_version" not in payload:
            raise EvalRunFileError(
                f"Unversioned eval run {resolved}; regenerate it with MCP Doctor V0.3."
            )
        run = EvalRun.model_validate(payload)
    except OSError as exc:
        raise EvalRunFileError(f"Could not read eval run {resolved}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalRunFileError(f"Invalid JSON eval run {resolved}: {exc}") from exc
    except ValidationError as exc:
        raise EvalRunFileError(f"Invalid eval run {resolved}: {exc}") from exc
    if run.eval_run_format_version != 1:
        raise EvalRunFileError(
            f"Unsupported eval run format version {run.eval_run_format_version} in {resolved}."
        )
    return run
