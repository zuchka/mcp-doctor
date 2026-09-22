from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from mcp_doctor.lab.models import LabError

T = TypeVar("T", bound=BaseModel)


def canonical(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LabError(f"Could not read {path}: {exc}") from exc


def load_json(path: Path, model: type[T]) -> T:
    try:
        return model.model_validate_json(read_bytes(path))
    except ValidationError as exc:
        raise LabError(f"Incompatible or invalid {path}: {exc}") from exc


def load_toml(path: Path, model: type[T]) -> T:
    try:
        data = tomllib.loads(read_bytes(path).decode())
        return model.model_validate(data)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise LabError(f"Invalid Lab manifest {path}: {exc}") from exc


def write_bytes(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Durable atomic write; exclusive mode never replaces an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            Path(temporary).unlink()
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, FileExistsError) as exc:
        raise LabError(f"Could not write {path}: {exc}") from exc
    finally:
        if temporary:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> str:
    data = canonical(value)
    write_bytes(path, data, exclusive=exclusive)
    return sha256(data)
