#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import codex_app_protocol


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    repo: Path
    cwd: Path
    capsule_path: Path
    continuation_prompt: str
    source_session_id: str
    delivery_id: str
    transfer_id: str
    state_path: Path
    codex_binary: Path
    response_timeout: float = 30.0
    turn_timeout: float = 3600.0


@dataclass(frozen=True, slots=True)
class LaunchResult:
    acknowledged: bool
    deduplicated: bool
    destination_thread_id: str | None
    destination_turn_id: str | None
    status: str
    error: str | None


def validate(config: DeliveryConfig) -> DeliveryConfig:
    repo = config.repo.resolve()
    cwd = config.cwd.resolve()
    capsule = config.capsule_path.resolve()
    if cwd != repo:
        raise codex_app_protocol.AppServerFailure(
            code="cwd_mismatch",
            detail="destination cwd must equal the source repository",
        )
    if not capsule.is_file():
        raise codex_app_protocol.AppServerFailure(
            code="capsule_missing",
            detail=str(capsule),
        )
    if not config.continuation_prompt:
        raise codex_app_protocol.AppServerFailure(
            code="prompt_missing",
            detail="continuation prompt must not be empty",
        )
    if (
        not math.isfinite(config.response_timeout)
        or config.response_timeout <= 0
        or not math.isfinite(config.turn_timeout)
        or config.turn_timeout <= 0
    ):
        raise codex_app_protocol.AppServerFailure(
            code="invalid_timeout",
            detail="protocol timeouts must be finite positive seconds",
        )
    return DeliveryConfig(
        repo=repo,
        cwd=cwd,
        capsule_path=capsule,
        continuation_prompt=config.continuation_prompt,
        source_session_id=config.source_session_id,
        delivery_id=config.delivery_id,
        transfer_id=config.transfer_id,
        state_path=config.state_path.resolve(),
        codex_binary=config.codex_binary.resolve(),
        response_timeout=config.response_timeout,
        turn_timeout=config.turn_timeout,
    )


def build(
    config: DeliveryConfig,
    *,
    status: str = "launching",
    destination_thread_id: str | None = None,
    destination_turn_id: str | None = None,
    destination_readable: bool = False,
    acknowledged: bool | None = None,
    error: str | None = None,
) -> JsonObject:
    existing = read(config.state_path) or {}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    same_delivery = existing.get("delivery_id") == config.delivery_id
    created_at = existing.get("created_at") if same_delivery else None
    existing_thread = existing.get("destination_thread_id") if same_delivery else None
    existing_turn = existing.get("destination_turn_id") if same_delivery else None
    is_acknowledged = (
        acknowledged
        if acknowledged is not None
        else same_delivery and existing.get("acknowledged") is True
    )
    return {
        "version": 1,
        "source_session_id": config.source_session_id,
        "delivery_id": config.delivery_id,
        "transfer_id": config.transfer_id,
        "destination_thread_id": (
            destination_thread_id
            if destination_thread_id is not None
            else existing_thread
        ),
        "destination_turn_id": (
            destination_turn_id if destination_turn_id is not None else existing_turn
        ),
        "capsule_path": str(config.capsule_path),
        "continuation_prompt": config.continuation_prompt,
        "cwd": str(config.cwd),
        "status": status,
        "destination_readable": destination_readable,
        "acknowledged": is_acknowledged,
        "delivered": is_acknowledged and status in {"running", "completed"},
        "created_at": created_at if isinstance(created_at, str) else now,
        "updated_at": now,
        "timestamp": dt.datetime.now(dt.timezone.utc).timestamp(),
        "error": error,
    }


def result(state: JsonObject, deduplicated: bool) -> LaunchResult:
    thread_id = state.get("destination_thread_id")
    turn_id = state.get("destination_turn_id")
    error = state.get("error")
    return LaunchResult(
        acknowledged=(
            state.get("status") in {"running", "completed"}
            and
            state.get("acknowledged") is True
            and isinstance(thread_id, str)
            and bool(thread_id)
            and isinstance(turn_id, str)
            and bool(turn_id)
        ),
        deduplicated=deduplicated,
        destination_thread_id=thread_id if isinstance(thread_id, str) else None,
        destination_turn_id=turn_id if isinstance(turn_id, str) else None,
        status=str(state.get("status") or "launching"),
        error=error if isinstance(error, str) else None,
    )


@contextmanager
def locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write(path: Path, payload: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read(path: Path) -> JsonObject | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise codex_app_protocol.AppServerFailure(
            code="delivery_state_unreadable",
            detail=f"{path}: {error}",
        ) from error
    decoded = decode(raw)
    if decoded is None:
        raise codex_app_protocol.AppServerFailure(
            code="delivery_state_corrupt",
            detail=str(path),
        )
    return decoded


def decode(raw: str) -> JsonObject | None:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def config_json(config: DeliveryConfig) -> JsonObject:
    return {
        "repo": str(config.repo),
        "cwd": str(config.cwd),
        "capsule_path": str(config.capsule_path),
        "continuation_prompt": config.continuation_prompt,
        "source_session_id": config.source_session_id,
        "delivery_id": config.delivery_id,
        "transfer_id": config.transfer_id,
        "state_path": str(config.state_path),
        "codex_binary": str(config.codex_binary),
        "response_timeout": config.response_timeout,
        "turn_timeout": config.turn_timeout,
    }


def config_from_json(payload: JsonObject) -> DeliveryConfig:
    return DeliveryConfig(
        repo=Path(str(payload["repo"])),
        cwd=Path(str(payload["cwd"])),
        capsule_path=Path(str(payload["capsule_path"])),
        continuation_prompt=str(payload["continuation_prompt"]),
        source_session_id=str(payload["source_session_id"]),
        delivery_id=str(payload["delivery_id"]),
        transfer_id=str(payload["transfer_id"]),
        state_path=Path(str(payload["state_path"])),
        codex_binary=Path(str(payload["codex_binary"])),
        response_timeout=_float_value(payload, "response_timeout", 30.0),
        turn_timeout=_float_value(payload, "turn_timeout", 3600.0),
    )


def _float_value(
    payload: JsonObject,
    key: str,
    default: float,
) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise codex_app_protocol.AppServerFailure(
            code="invalid_worker_request",
            detail=f"{key} must be numeric",
        )
    return float(value)
