"""Strict current-schema JSON storage with atomic, serialized transactions."""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import UserFacingError

JSON_MAX_DEPTH = 64


class StorageError(RuntimeError):
    """Data cannot be read or saved without risking loss."""


class StorageValidationError(UserFacingError, ValueError):
    """A transaction exceeded store limits or violated the current schema."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def strict_json_loads(raw: str | bytes | bytearray) -> Any:
    """Reject ambiguity, nonfinite numbers and excessive container nesting."""
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_json_float,
        )
    except RecursionError:
        raise ValueError("JSON nesting exceeds depth limit") from None
    if isinstance(payload, (dict, list)):
        # Retain one iterator per container depth, not all siblings in wide objects.
        stack = [iter(payload.values() if isinstance(payload, dict) else payload)]
        while stack:
            try:
                value = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            if isinstance(value, (dict, list)):
                if len(stack) >= JSON_MAX_DEPTH:
                    raise ValueError("JSON nesting exceeds depth limit")
                stack.append(iter(value.values() if isinstance(value, dict) else value))
    return payload


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            if stream.tell() > 32 * 1024 * 1024:
                raise ValueError("存储文件达到 32 MiB 上限；原数据未修改。")
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonStore:
    def __init__(self, path: Path, default: dict, validator: Callable[[dict], bool]):
        self.path = Path(path)
        self.validator = validator
        self._lock = threading.RLock()
        self._parent_real = self.path.parent.resolve()
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise StorageError(f"{self.path.name}: invalid store path")
        self._payload = copy.deepcopy(default)
        if not self.path.exists():
            if any(self.path.parent.glob(f"{self.path.name}.corrupt-*")):
                raise StorageError(
                    f"{self.path.name}: resolve the saved corrupt backup before creating a new store"
                )
            return
        try:
            if self.path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("store exceeds file size limit")
            payload = strict_json_loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not validator(payload):
                raise ValueError("invalid current schema")
        except (ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{uuid.uuid4().hex}")
            try:
                os.replace(self.path, backup)
            except OSError as backup_error:
                raise StorageError(f"{self.path.name}: unreadable; backup failed") from backup_error
            raise StorageError(
                f"{self.path.name}: invalid data backed up as {backup.name}"
            ) from exc
        self._payload = payload

    def snapshot(self, *keys: str, default: Any = None) -> Any:
        # Publish a new object only after a successful atomic write. Readers never
        # wait for disk I/O or observe an in-progress mutation.
        payload = self._payload
        for key in keys:
            if not isinstance(payload, dict) or key not in payload:
                return copy.deepcopy(default)
            payload = payload[key]
        return copy.deepcopy(payload)

    def update(self, mutate: Callable[[dict], Any]) -> Any:
        with self._lock:
            try:
                safe_path = (
                    self.path.parent.resolve() == self._parent_real
                    and not self.path.is_symlink()
                    and not self.path.is_junction()
                    and (not self.path.exists() or self.path.is_file())
                )
            except (OSError, RuntimeError):
                safe_path = False
            if not safe_path:
                raise StorageError(f"{self.path.name}: invalid store path")
            payload = self.snapshot()
            result = mutate(payload)
            if not self.validator(payload):
                raise StorageValidationError("存储容量达到上限或数据无效；原数据未修改。")
            if payload != self._payload:
                atomic_write(self.path, payload)
                self._payload = payload
            return result
