#!/usr/bin/env python3
"""Durable acknowledgement and sole-writer transfer control.

This module intentionally uses only the Python standard library.  The ownership
record is authoritative; transfer records and active pointers are projections
that can be repaired after an interrupted acknowledgement.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import errno
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Iterator, Mapping, Sequence


try:
    fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - the supported Codex runtimes are POSIX.
    fcntl = None


STATE_DIR_NAME = "checkpoint-and-continue"
TRANSFER_VERSION = 1
PHASES = (
    "prepared",
    "delivered",
    "clean_session_started",
    "resume_verified",
    "acknowledged",
    "source_stop_requested",
    "source_quiesced",
)
PHASE_INDEX = {phase: index for index, phase in enumerate(PHASES)}
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
FINAL_STOP_RESULTS = {"interrupted", "quiesced", "already_exited"}
PENDING_STOP_RESULTS = {"unsupported", "failed", "termination_pending"}
FINAL_STOP_EVIDENCE = {
    ("native_interrupt", "interrupted"): "native_interrupt_result",
    ("native_interrupt", "already_exited"): "native_exit_observation",
    ("cooperative", "quiesced"): "cooperative_quiescence",
    ("cooperative", "already_exited"): "cooperative_exit_observation",
}
STOP_CAPABILITIES = {
    "native_interrupt",
    "cooperative",
    "hook_read_only",
    "process_group_interruption_unavailable",
    "unsupported",
}
# Safe substitute until a host adapter can persist and revalidate leader PID,
# PGID, process start identity, and runtime-registration provenance.  The core
# deliberately exposes evidence of unavailability and contains no kill path.
PROCESS_GROUP_INTERRUPTION = {
    "available": False,
    "reason": "host_adapter_must_prove_leader_pid_pgid_start_and_runtime_tokens",
}
_AUTHORITY_LOCAL = threading.local()
FAILURE_CODES = {
    "clean_session_launch_failed",
    "launch_outcome_unknown",
    "capsule_verification_failed",
    "acknowledgement_timed_out",
    "stop_request_unsupported",
    "stop_request_failed",
    "source_termination_pending",
    "ownership_conflict",
    "stale_acknowledgement",
    "duplicate_acknowledgement",
    "replayed_acknowledgement",
    "cross_session_acknowledgement",
}
FAULT_ENV = "CHECKPOINT_AND_CONTINUE_TRANSFER_FAULT"


class TransferError(RuntimeError):
    """A state-machine rejection with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FaultInjected(RuntimeError):
    """Raised by deterministic durability-boundary tests."""


@dataclass(frozen=True)
class TransferPaths:
    root: Path
    lock: Path
    ownership: Path
    source_scope: str
    session_dir: Path
    tombstone: Path
    active: Path
    transfers: Path


@dataclass(frozen=True)
class WriteFence:
    """Authority snapshot valid only while the transfer lock remains held."""

    actor_session_id: str
    ownership_epoch: int
    ownership_digest: str
    reason: str


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def session_scope(session_id: str) -> str:
    stable = session_id.strip()
    if not stable:
        raise TransferError("invalid_identity", "source session ID must not be empty")
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def state_root(repo: Path) -> Path:
    return repo.resolve() / ".omx" / "state" / STATE_DIR_NAME


def transfer_paths(repo: Path, source_session_id: str) -> TransferPaths:
    root = state_root(repo)
    scope = session_scope(source_session_id)
    session_dir = root / "sessions" / scope
    return TransferPaths(
        root=root,
        lock=root / ".transfer.lock",
        ownership=root / ".ownership.json",
        source_scope=scope,
        session_dir=session_dir,
        tombstone=session_dir / ".revoked.json",
        active=session_dir / ".active-transfer.json",
        transfers=session_dir / "transfers",
    )


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _fsync_parent(path: Path) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path.parent), flags)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}:
            return False
        raise
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}:
            return False
        raise
    finally:
        os.close(descriptor)
    return True


def durable_write_json(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically replace JSON, fsync it, and verify the exact persisted digest."""

    materialized = dict(payload)
    durability = dict(materialized.get("durability", {}))
    durability.update(
        {
            "atomic_replace": True,
            "file_fsync": True,
            "parent_directory_fsync": "complete",
            "readback_verified": True,
        }
    )
    materialized["durability"] = durability
    path.parent.mkdir(parents=True, exist_ok=True)

    def replace(value: Mapping[str, Any]) -> tuple[bytes, bool]:
        encoded = _canonical_bytes(value)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{time.time_ns()}.{secrets.token_hex(4)}.tmp"
        )
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            parent_synced = _fsync_parent(path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return encoded, parent_synced

    expected, parent_synced = replace(materialized)
    if not parent_synced:
        materialized["durability"][
            "parent_directory_fsync"
        ] = "unsupported_readback_verified"
        expected, _ignored = replace(materialized)
    actual = path.read_bytes()
    if actual != expected:
        raise OSError(f"durable read-back mismatch for {path}")
    return materialized


def _load_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise TransferError("missing_state", f"required state is missing: {path}")
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransferError("corrupt_state", f"cannot read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise TransferError("corrupt_state", f"state is not an object: {path}")
    return payload


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise TransferError(
            "locking_unavailable",
            "durable transfer locking is unavailable; refusing an unsafe transition",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fault(point: str) -> None:
    if os.environ.get(FAULT_ENV, "").strip() == point:
        raise FaultInjected(f"fault injected at {point}")


def _require_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise TransferError("invalid_identity", f"{name} must not be empty")
    return normalized


def _validate_nonce(nonce: str) -> str:
    if not NONCE_PATTERN.fullmatch(nonce):
        raise TransferError(
            "invalid_identity",
            "nonce must be 22-128 URL-safe base64 characters",
        )
    return nonce


def _validate_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise TransferError("invalid_identity", "capsule SHA-256 must be 64 lowercase hex characters")
    return normalized


def _transfer_id(revision: int, nonce: str) -> str:
    nonce_hash = hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:16]
    return f"r{revision}-{nonce_hash}"


def derive_transfer_id(revision: int, nonce: str) -> str:
    """Return the public deterministic identity used by capsules and journals."""

    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise TransferError("invalid_identity", "capsule revision must be positive")
    return _transfer_id(revision, _validate_nonce(nonce))


def _record_path(paths: TransferPaths, transfer_id: str) -> Path:
    if not re.fullmatch(r"r[1-9][0-9]*-[0-9a-f]{16}", transfer_id):
        raise TransferError("invalid_identity", "invalid transfer ID")
    return paths.transfers / f"{transfer_id}.json"


def _validate_capsule_path(paths: TransferPaths, capsule_path: str) -> str:
    """Return a canonical capsule path contained in the source session root."""

    supplied = Path(_require_text("capsule path", capsule_path)).expanduser()
    try:
        metadata = supplied.lstat()
        resolved = supplied.resolve(strict=True)
        session_root = paths.session_dir.resolve(strict=False)
        repo_root = paths.root.parents[2].resolve(strict=True)
        session_root.relative_to(repo_root)
        resolved.relative_to(session_root)
    except (OSError, ValueError) as error:
        raise TransferError(
            "unsafe_capsule_path",
            "capsule must be a real file contained in the canonical source session root",
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise TransferError("unsafe_capsule_path", "capsule symlinks and non-regular files are rejected")
    return str(resolved)


def _identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "transfer_id",
            "source_session_id",
            "source_scope",
            "destination_session_id",
            "destination_task_id",
            "goal_identity",
            "capsule_path",
            "capsule_revision",
            "capsule_sha256",
            "resume_ready",
            "nonce",
        )
    }


def _validate_record(paths: TransferPaths, record: Mapping[str, Any]) -> None:
    required_text = (
        "transfer_id",
        "source_session_id",
        "source_scope",
        "goal_identity",
        "capsule_path",
        "capsule_sha256",
        "nonce",
        "phase",
    )
    if any(not isinstance(record.get(key), str) or not str(record[key]).strip() for key in required_text):
        raise TransferError("corrupt_state", "transfer record has incomplete identity")
    if record.get("source_scope") != paths.source_scope:
        raise TransferError("corrupt_state", "transfer record source scope mismatch")
    if session_scope(str(record["source_session_id"])) != paths.source_scope:
        raise TransferError("corrupt_state", "transfer record source session/scope mismatch")
    revision = record.get("capsule_revision")
    if not isinstance(revision, int) or revision < 1:
        raise TransferError("corrupt_state", "transfer record has invalid revision")
    try:
        nonce = _validate_nonce(str(record["nonce"]))
        _validate_sha256(str(record["capsule_sha256"]))
    except TransferError as error:
        raise TransferError("corrupt_state", str(error)) from error
    if record.get("transfer_id") != _transfer_id(revision, nonce):
        raise TransferError("corrupt_state", "transfer ID does not match revision and nonce")
    if record.get("phase") not in PHASE_INDEX or record.get("resume_ready") is not True:
        raise TransferError("corrupt_state", "transfer record phase/readiness is invalid")
    if PHASE_INDEX[str(record["phase"])] >= PHASE_INDEX["clean_session_started"]:
        destination_session = record.get("destination_session_id")
        destination_task = record.get("destination_task_id")
        if (
            not isinstance(destination_session, str)
            or not destination_session
            or not isinstance(destination_task, str)
            or not destination_task
            or destination_session == record.get("source_session_id")
        ):
            raise TransferError("corrupt_state", "transfer destination identity is invalid")
    expected_path = _record_path(paths, str(record["transfer_id"])).resolve(strict=False)
    if expected_path.parent != paths.transfers.resolve(strict=False):
        raise TransferError("corrupt_state", "transfer record path escapes its session")


def _append_event(record: dict[str, Any], event: str, **details: Any) -> None:
    events = record.setdefault("events", [])
    events.append({"event": event, "at": _now(), **details})
    record["updated_at"] = events[-1]["at"]


def _write_record(paths: TransferPaths, record: dict[str, Any]) -> dict[str, Any]:
    written = durable_write_json(_record_path(paths, str(record["transfer_id"])), record)
    record.clear()
    record.update(written)
    return record


def _write_pointer(paths: TransferPaths, record: Mapping[str, Any]) -> dict[str, Any]:
    pointer = {
        "version": TRANSFER_VERSION,
        **_identity(record),
        "phase": record["phase"],
        "record_path": str(_record_path(paths, str(record["transfer_id"]))),
        "updated_at": _now(),
    }
    return durable_write_json(paths.active, pointer)


def _retained_records(paths: TransferPaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for candidate_path in paths.transfers.glob("r*-*.json"):
        candidate = _load_json(candidate_path, required=True)
        _validate_record(paths, candidate)
        if candidate_path.resolve(strict=True) != _record_path(
            paths, str(candidate["transfer_id"])
        ).resolve(strict=True):
            raise TransferError("corrupt_state", "transfer record filename/identity mismatch")
        records.append(candidate)
    return records


def _active_record(paths: TransferPaths) -> dict[str, Any]:
    pointer = _load_json(paths.active, required=True)
    if pointer.get("source_scope") != paths.source_scope:
        raise TransferError("corrupt_state", "active pointer source scope mismatch")
    record_path = _record_path(paths, str(pointer.get("transfer_id", "")))
    if pointer.get("record_path") != str(record_path):
        raise TransferError("corrupt_state", "active pointer record path mismatch")
    record = _load_json(record_path, required=True)
    _validate_record(paths, record)
    for key, value in _identity(record).items():
        if pointer.get(key) != value:
            raise TransferError("corrupt_state", f"active pointer identity mismatch: {key}")
    pointer_phase = str(pointer.get("phase", ""))
    record_phase = str(record.get("phase", ""))
    if pointer_phase != record_phase:
        if (
            pointer_phase not in PHASE_INDEX
            or PHASE_INDEX[pointer_phase] > PHASE_INDEX[record_phase]
        ):
            raise TransferError("corrupt_state", "active pointer phase mismatch")
        _write_pointer(paths, record)
    highest_revision = int(record["capsule_revision"])
    highest_transfer = str(record["transfer_id"])
    for candidate in _retained_records(paths):
        revision = int(candidate["capsule_revision"])
        if revision > highest_revision:
            highest_revision = revision
            highest_transfer = str(candidate["transfer_id"])
        elif revision == highest_revision and candidate["transfer_id"] != highest_transfer:
            raise TransferError("corrupt_state", "multiple transfer records claim the latest revision")
    if highest_transfer != record["transfer_id"]:
        raise TransferError("pointer_rollback", "active pointer was rolled back to a predecessor")
    return record


def _set_phase(record: dict[str, Any], phase: str) -> None:
    current = str(record.get("phase", ""))
    if phase not in PHASE_INDEX or current not in PHASE_INDEX:
        raise TransferError("invalid_transition", f"unknown phase transition {current!r} -> {phase!r}")
    if PHASE_INDEX[phase] < PHASE_INDEX[current]:
        raise TransferError("invalid_transition", f"cannot move backwards from {current} to {phase}")
    if PHASE_INDEX[phase] > PHASE_INDEX[current] + 1:
        raise TransferError("invalid_transition", f"cannot skip from {current} to {phase}")
    record["phase"] = phase


def _failure(record: dict[str, Any], code: str, detail: str) -> None:
    if code not in FAILURE_CODES:
        raise TransferError("invalid_failure", f"unsupported failure code: {code}")
    record["failure"] = {"code": code, "detail": detail, "at": _now()}
    _append_event(record, "failure", code=code, detail=detail)


def _clear_failure(record: dict[str, Any], *codes: str) -> None:
    failure = record.get("failure")
    if isinstance(failure, dict) and failure.get("code") in codes:
        record["failure"] = None


def _ownership_can_continue(ownership: Mapping[str, Any]) -> bool:
    stop = ownership.get("source_stop")
    if not isinstance(stop, dict):
        return False
    return bool(
        stop.get("observed_quiesced")
        or (stop.get("termination_pending") and stop.get("enforced_read_only"))
    )


def _source_may_prepare(paths: TransferPaths, source_session_id: str) -> None:
    if paths.tombstone.exists():
        raise TransferError("source_revoked", "source has a durable revocation tombstone")
    ownership = _load_json(paths.ownership)
    if not ownership:
        return
    ownership_paths = transfer_paths(paths.root.parent.parent.parent, str(ownership.get("source_session_id", "")))
    _validate_ownership(ownership_paths, ownership)
    if (
        ownership.get("destination_session_id") == source_session_id
        and _ownership_can_continue(ownership)
    ):
        return
    raise TransferError("ownership_conflict", "source does not hold current write ownership")


def prepare(
    repo: Path,
    *,
    source_session_id: str,
    goal_identity: str,
    capsule_path: str,
    capsule_revision: int,
    capsule_sha256: str,
    resume_ready: bool,
    nonce: str = "",
) -> dict[str, Any]:
    source_session_id = _require_text("source session ID", source_session_id)
    goal_identity = _require_text("goal identity", goal_identity)
    if capsule_revision < 1:
        raise TransferError("invalid_identity", "capsule revision must be positive")
    capsule_sha256 = _validate_sha256(capsule_sha256)
    if not resume_ready:
        raise TransferError("capsule_not_ready", "a non-ready capsule cannot be transferred")
    nonce = _validate_nonce(nonce or secrets.token_urlsafe(24))
    paths = transfer_paths(repo, source_session_id)
    capsule_path = _validate_capsule_path(paths, capsule_path)
    transfer_id = _transfer_id(capsule_revision, nonce)
    with _locked(paths.lock):
        _source_may_prepare(paths, source_session_id)
        if paths.active.exists():
            active = _active_record(paths)
            same_revision = active.get("capsule_revision") == capsule_revision
            same_capsule = (
                active.get("capsule_sha256") == capsule_sha256
                and active.get("capsule_path") == capsule_path
                and active.get("goal_identity") == goal_identity
            )
            if same_revision and same_capsule:
                return _result(active, idempotent=True)
            if int(active.get("capsule_revision", 0)) >= capsule_revision:
                raise TransferError("revision_conflict", "active transfer is not older than this capsule")
        else:
            retained = _retained_records(paths)
            same_revision = [
                record
                for record in retained
                if record.get("capsule_revision") == capsule_revision
            ]
            exact_orphans = [
                record
                for record in same_revision
                if record.get("goal_identity") == goal_identity
                and record.get("capsule_path") == capsule_path
                and record.get("capsule_sha256") == capsule_sha256
                and record.get("resume_ready") is True
            ]
            if len(exact_orphans) == 1 and len(same_revision) == 1:
                orphan = exact_orphans[0]
                _write_pointer(paths, orphan)
                return _result(orphan, idempotent=True)
            if same_revision:
                code = "orphan_conflict" if len(same_revision) > 1 else "revision_conflict"
                raise TransferError(code, "retained transfer conflicts with requested revision")
            if retained and max(int(record["capsule_revision"]) for record in retained) > capsule_revision:
                raise TransferError("revision_conflict", "a newer retained transfer already exists")
        record = {
            "version": TRANSFER_VERSION,
            "transfer_id": transfer_id,
            "source_session_id": source_session_id,
            "source_scope": paths.source_scope,
            "destination_session_id": None,
            "destination_task_id": None,
            "goal_identity": goal_identity,
            "capsule_path": capsule_path,
            "capsule_revision": capsule_revision,
            "capsule_sha256": capsule_sha256,
            "resume_ready": True,
            "nonce": nonce,
            "phase": "prepared",
            "launch": {"status": "not_requested", "retry_count": 0},
            "delivery": None,
            "verification": None,
            "acknowledgement": None,
            "stop": None,
            "failure": None,
            "created_at": _now(),
            "events": [],
        }
        _append_event(record, "prepared")
        _write_record(paths, record)
        _fault("after_prepare_record_before_pointer")
        _write_pointer(paths, record)
        return _result(record)


def _load_exact(repo: Path, source_session_id: str, transfer_id: str) -> tuple[TransferPaths, dict[str, Any]]:
    paths = transfer_paths(repo, source_session_id)
    record = _load_json(_record_path(paths, transfer_id), required=True)
    if record.get("source_session_id") != source_session_id:
        raise TransferError("cross_session_acknowledgement", "source session does not match transfer")
    return paths, record


def launch_requested(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    transport_key: str,
) -> dict[str, Any]:
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    transport_key = _require_text("transport key", transport_key)
    with _locked(paths.lock):
        record = _active_record(paths)
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_transfer", "transfer is no longer active")
        launch = record["launch"]
        if launch.get("status") in {"requested", "outcome_unknown"}:
            if launch.get("transport_key") == transport_key:
                return _result(record, idempotent=True)
            raise TransferError(
                "launch_outcome_unknown",
                "a launch is already pending; reconcile it before another create",
            )
        if launch.get("status") in {"reconciled", "succeeded"}:
            if launch.get("transport_key") == transport_key:
                return _result(record, idempotent=True)
            raise TransferError("duplicate_destination", "transfer already has a destination")
        retry_count = int(launch.get("retry_count", 0))
        if launch.get("status") == "failed":
            retry_count += 1
        if retry_count > 3:
            raise TransferError("launch_retry_exhausted", "clean-session launch retry limit reached")
        record["launch"] = {
            "status": "requested",
            "transport_key": transport_key,
            "requested_at": _now(),
            "retry_count": retry_count,
        }
        _clear_failure(record, "clean_session_launch_failed", "launch_outcome_unknown")
        _append_event(record, "launch_requested", transport_key=transport_key)
        _write_record(paths, record)
        _write_pointer(paths, record)
        return _result(record)


def record_launch_outcome(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    outcome: str,
    detail: str = "",
) -> dict[str, Any]:
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    if outcome not in {"failed", "unknown"}:
        raise TransferError("invalid_launch_outcome", "outcome must be failed or unknown")
    with _locked(paths.lock):
        record = _active_record(paths)
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_transfer", "transfer is no longer active")
        launch = record["launch"]
        if launch.get("status") not in {"requested", "failed", "outcome_unknown"}:
            raise TransferError("invalid_transition", "launch outcome is not pending")
        status = "failed" if outcome == "failed" else "outcome_unknown"
        launch["status"] = status
        launch["outcome_at"] = _now()
        launch["detail"] = detail
        code = "clean_session_launch_failed" if outcome == "failed" else "launch_outcome_unknown"
        _failure(record, code, detail or status)
        _write_record(paths, record)
        _write_pointer(paths, record)
        return _result(record)


def delivered(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    transport_key: str,
    destination_task_id: str,
) -> dict[str, Any]:
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    transport_key = _require_text("transport key", transport_key)
    destination_task_id = _require_text("destination task ID", destination_task_id)
    with _locked(paths.lock):
        record = _active_record(paths)
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_transfer", "transfer is no longer active")
        existing = record.get("delivery")
        if isinstance(existing, dict):
            if (
                existing.get("transport_key") == transport_key
                and existing.get("destination_task_id") == destination_task_id
            ):
                return _result(record, idempotent=True)
            raise TransferError("duplicate_destination", "a distinct delivery is already recorded")
        launch = record.get("launch", {})
        if launch.get("status") == "reconciled" and (
            launch.get("reconciled_destination_task_id") != destination_task_id
        ):
            raise TransferError("duplicate_destination", "delivery differs from reconciled destination")
        if launch.get("transport_key") != transport_key or launch.get("status") not in {
            "requested",
            "reconciled",
        }:
            if launch.get("status") == "outcome_unknown":
                raise TransferError(
                    "launch_outcome_unknown",
                    "unknown launch outcome requires exact nonce reconciliation before delivery",
                )
            raise TransferError("invalid_transition", "delivery does not match launch intent")
        _set_phase(record, "delivered")
        record["delivery"] = {
            "transport_key": transport_key,
            "destination_task_id": destination_task_id,
            "delivered_at": _now(),
        }
        launch["status"] = "succeeded"
        launch["destination_task_id"] = destination_task_id
        _clear_failure(record, "launch_outcome_unknown")
        _append_event(record, "delivered", destination_task_id=destination_task_id)
        _write_record(paths, record)
        _write_pointer(paths, record)
        return _result(record)


def started(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    destination_session_id: str,
    destination_task_id: str,
) -> dict[str, Any]:
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    destination_session_id = _require_text("destination session ID", destination_session_id)
    destination_task_id = _require_text("destination task ID", destination_task_id)
    if destination_session_id == source_session_id:
        raise TransferError("invalid_destination", "source and destination sessions must differ")
    with _locked(paths.lock):
        record = _active_record(paths)
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_transfer", "transfer is no longer active")
        launch = record.get("launch", {})
        if launch.get("status") == "reconciled" and (
            launch.get("reconciled_destination_session_id") != destination_session_id
            or launch.get("reconciled_destination_task_id") != destination_task_id
        ):
            raise TransferError("duplicate_destination", "start differs from reconciled destination")
        if PHASE_INDEX.get(str(record.get("phase")), -1) >= PHASE_INDEX["clean_session_started"]:
            if (
                record.get("destination_session_id") == destination_session_id
                and record.get("destination_task_id") == destination_task_id
            ):
                return _result(record, idempotent=True)
            raise TransferError("duplicate_destination", "a distinct destination is already bound")
        delivery = record.get("delivery") or {}
        if delivery.get("destination_task_id") != destination_task_id:
            raise TransferError("cross_session_acknowledgement", "destination task does not match delivery")
        _set_phase(record, "clean_session_started")
        record["destination_session_id"] = destination_session_id
        record["destination_task_id"] = destination_task_id
        _append_event(
            record,
            "clean_session_started",
            destination_session_id=destination_session_id,
            destination_task_id=destination_task_id,
        )
        _write_record(paths, record)
        _write_pointer(paths, record)
        return _result(record)


def reconcile_launch(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    transport_key: str,
    observed_nonce: str,
    destination_session_id: str,
    destination_task_id: str,
) -> dict[str, Any]:
    transport_key = _require_text("transport key", transport_key)
    observed_nonce = _validate_nonce(observed_nonce)
    destination_session_id = _require_text("destination session ID", destination_session_id)
    destination_task_id = _require_text("destination task ID", destination_task_id)
    if destination_session_id == source_session_id:
        raise TransferError("invalid_destination", "source and destination sessions must differ")
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    with _locked(paths.lock):
        record = _active_record(paths)
        launch = record.get("launch", {})
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_transfer", "transfer is no longer active")
        if launch.get("status") == "reconciled":
            if (
                launch.get("transport_key") == transport_key
                and launch.get("reconciled_destination_session_id")
                == destination_session_id
                and launch.get("reconciled_destination_task_id") == destination_task_id
                and record.get("nonce") == observed_nonce
            ):
                return _result(record, idempotent=True)
            raise TransferError("duplicate_destination", "launch was reconciled to another destination")
        if launch.get("status") != "outcome_unknown":
            raise TransferError("invalid_transition", "launch outcome is not unknown")
        if launch.get("transport_key") != transport_key or record.get("nonce") != observed_nonce:
            raise TransferError("replayed_acknowledgement", "observed launch identity does not match")
        if record.get("phase") != "prepared" or record.get("delivery") is not None:
            raise TransferError("invalid_transition", "unknown launch record is already mutated")
        launch["status"] = "reconciled"
        launch["reconciled_at"] = _now()
        launch["reconciled_destination_session_id"] = destination_session_id
        launch["reconciled_destination_task_id"] = destination_task_id
        _append_event(record, "launch_reconciled", destination_task_id=destination_task_id)
        _set_phase(record, "delivered")
        record["delivery"] = {
            "transport_key": transport_key,
            "destination_task_id": destination_task_id,
            "delivered_at": _now(),
            "reconciled": True,
        }
        _append_event(record, "delivered", destination_task_id=destination_task_id)
        _set_phase(record, "clean_session_started")
        record["destination_session_id"] = destination_session_id
        record["destination_task_id"] = destination_task_id
        _clear_failure(record, "launch_outcome_unknown")
        _append_event(
            record,
            "clean_session_started",
            destination_session_id=destination_session_id,
            destination_task_id=destination_task_id,
        )
        _write_record(paths, record)
        _write_pointer(paths, record)
        return _result(record)


def _expected_identity(
    *,
    source_session_id: str,
    destination_session_id: str,
    destination_task_id: str,
    goal_identity: str,
    capsule_path: str,
    capsule_revision: int,
    capsule_sha256: str,
    nonce: str,
) -> dict[str, Any]:
    return {
        "source_session_id": source_session_id.strip(),
        "destination_session_id": destination_session_id.strip(),
        "destination_task_id": destination_task_id.strip(),
        "goal_identity": goal_identity.strip(),
        "capsule_path": str(Path(capsule_path).expanduser().resolve()),
        "capsule_revision": capsule_revision,
        "capsule_sha256": capsule_sha256.strip().lower(),
        "nonce": nonce.strip(),
    }


def _validate_exact(record: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    mismatches = [key for key, value in expected.items() if record.get(key) != value]
    if mismatches:
        session_keys = {"source_session_id", "destination_session_id", "destination_task_id"}
        code = (
            "cross_session_acknowledgement"
            if session_keys.intersection(mismatches)
            else "replayed_acknowledgement"
        )
        raise TransferError(code, f"identity mismatch: {', '.join(mismatches)}")


def _capsule_matches(paths: TransferPaths, record: Mapping[str, Any]) -> bool:
    try:
        canonical = _validate_capsule_path(paths, str(record["capsule_path"]))
        content = Path(canonical).read_bytes()
    except (OSError, TransferError):
        return False
    return hashlib.sha256(content).hexdigest() == record.get("capsule_sha256")


def verify(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    destination_session_id: str,
    destination_task_id: str,
    goal_identity: str,
    capsule_path: str,
    capsule_revision: int,
    capsule_sha256: str,
    nonce: str,
    repository_inspected: bool,
    goal_inspected: bool,
    exact_next_action: str,
    smallest_validation: str,
) -> dict[str, Any]:
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    expected = _expected_identity(
        source_session_id=source_session_id,
        destination_session_id=destination_session_id,
        destination_task_id=destination_task_id,
        goal_identity=goal_identity,
        capsule_path=capsule_path,
        capsule_revision=capsule_revision,
        capsule_sha256=_validate_sha256(capsule_sha256),
        nonce=_validate_nonce(nonce),
    )
    with _locked(paths.lock):
        record = _active_record(paths)
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_acknowledgement", "transfer is no longer active")
        try:
            _validate_exact(record, expected)
            checks_passed = bool(
                record.get("resume_ready")
                and repository_inspected
                and goal_inspected
                and exact_next_action.strip()
                and smallest_validation.strip()
                and _capsule_matches(paths, record)
            )
        except TransferError:
            raise
        if not checks_passed:
            record["verification"] = {
                "result": "failed",
                "verified_at": _now(),
                "repository_inspected": bool(repository_inspected),
                "goal_inspected": bool(goal_inspected),
            }
            _failure(record, "capsule_verification_failed", "destination verification checks failed")
            _write_record(paths, record)
            _write_pointer(paths, record)
            raise TransferError("capsule_verification_failed", "destination verification checks failed")
        if PHASE_INDEX.get(str(record.get("phase")), -1) >= PHASE_INDEX["resume_verified"]:
            prior = record.get("verification") or {}
            if (
                prior.get("result") == "verified"
                and prior.get("repository_inspected") is True
                and prior.get("goal_inspected") is True
                and prior.get("exact_next_action") == exact_next_action.strip()
                and prior.get("smallest_validation") == smallest_validation.strip()
            ):
                return _result(record, idempotent=True)
            raise TransferError("replayed_acknowledgement", "verification content changed")
        _set_phase(record, "resume_verified")
        record["verification"] = {
            "result": "verified",
            "verified_at": _now(),
            "repository_inspected": True,
            "goal_inspected": True,
            "exact_next_action": exact_next_action.strip(),
            "smallest_validation": smallest_validation.strip(),
        }
        record["failure"] = None
        _append_event(record, "resume_verified")
        _write_record(paths, record)
        _write_pointer(paths, record)
        return _result(record)


def _receipt(record: Mapping[str, Any], acknowledged_at: str) -> dict[str, Any]:
    verification = record.get("verification") or {}
    receipt = {
        **_identity(record),
        "verification_digest": _digest(verification),
        "acknowledged_at": acknowledged_at,
    }
    receipt["receipt_digest"] = _digest(receipt)
    return receipt


def _validate_tombstone_receipt(
    paths: TransferPaths,
    tombstone: Mapping[str, Any],
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Recompute the exact receipt from the retained verified transfer record."""

    _validate_record(paths, record)
    verification = record.get("verification")
    if not isinstance(verification, dict) or verification.get("result") != "verified":
        raise TransferError("corrupt_ownership", "receipt record is not destination-verified")
    receipt = tombstone.get("receipt")
    if not isinstance(receipt, dict):
        raise TransferError("corrupt_ownership", "revocation receipt is missing")
    acknowledged_at = receipt.get("acknowledged_at")
    if not isinstance(acknowledged_at, str) or not acknowledged_at:
        raise TransferError("corrupt_ownership", "receipt acknowledgement time is missing")
    expected = _receipt(record, acknowledged_at)
    if receipt != expected:
        raise TransferError(
            "corrupt_ownership",
            "receipt does not exactly bind current identity and verification digest",
        )
    return dict(receipt)


def _validate_ownership(
    paths: TransferPaths,
    ownership: Mapping[str, Any],
    record: Mapping[str, Any] | None = None,
) -> None:
    if not ownership:
        raise TransferError("missing_state", "ownership state is missing")
    epoch = ownership.get("ownership_epoch")
    if not isinstance(epoch, int) or epoch < 1:
        raise TransferError("corrupt_ownership", "ownership epoch is invalid")
    required = {
        "transfer_id",
        "source_session_id",
        "source_scope",
        "destination_session_id",
        "destination_task_id",
        "sole_writer_session_id",
        "goal_identity",
        "capsule_path",
        "capsule_sha256",
        "nonce",
        "receipt_digest",
        "tombstone_digest",
        "acknowledged_at",
        "ownership_committed_at",
    }
    if any(not isinstance(ownership.get(key), str) or not ownership[key] for key in required):
        raise TransferError("corrupt_ownership", "ownership identity is incomplete")
    if ownership.get("source_scope") != session_scope(str(ownership["source_session_id"])):
        raise TransferError("corrupt_ownership", "ownership source scope is invalid")
    if ownership.get("source_scope") != paths.source_scope:
        raise TransferError("corrupt_ownership", "ownership belongs to another source scope")
    if (
        ownership.get("source_revoked") is not True
        or ownership.get("resume_ready") is not True
        or ownership.get("sole_writer_session_id") != ownership.get("destination_session_id")
        or ownership.get("source_session_id") == ownership.get("destination_session_id")
    ):
        raise TransferError("corrupt_ownership", "sole-writer ownership fields are inconsistent")
    revision = ownership.get("capsule_revision")
    if not isinstance(revision, int) or revision < 1:
        raise TransferError("corrupt_ownership", "ownership revision is invalid")
    try:
        _validate_sha256(str(ownership["capsule_sha256"]))
        _validate_nonce(str(ownership["nonce"]))
    except TransferError as error:
        raise TransferError("corrupt_ownership", str(error)) from error
    stop = ownership.get("source_stop")
    if not isinstance(stop, dict) or stop.get("enforced_read_only") is not True:
        raise TransferError("corrupt_ownership", "ownership lacks enforced source read-only state")
    stop_result = stop.get("result")
    if stop_result in FINAL_STOP_RESULTS:
        expected_kind = FINAL_STOP_EVIDENCE.get(
            (str(stop.get("capability", "")), str(stop_result))
        )
        evidence = stop.get("adapter_evidence")
        if (
            expected_kind is None
            or not isinstance(evidence, dict)
            or evidence.get("kind") != expected_kind
            or not isinstance(evidence.get("reference"), str)
            or not evidence["reference"]
            or not isinstance(evidence.get("recorded_at"), str)
            or not evidence["recorded_at"]
        ):
            raise TransferError("corrupt_ownership", "final stop result lacks compatible adapter evidence")
    elif isinstance(stop.get("adapter_evidence"), dict):
        raise TransferError("corrupt_ownership", "non-final stop state contains success evidence")

    tombstone = _load_json(paths.tombstone, required=True)
    if _digest(tombstone) != ownership.get("tombstone_digest"):
        raise TransferError("corrupt_ownership", "revocation tombstone digest mismatch")
    for key in ("source_session_id", "source_scope", "transfer_id", "nonce"):
        if tombstone.get(key) != ownership.get(key):
            raise TransferError("corrupt_ownership", f"revocation identity mismatch: {key}")
    if tombstone.get("enforced_read_only") is not True:
        raise TransferError("corrupt_ownership", "revocation tombstone is not read-only")
    bound_record = record
    if bound_record is None:
        bound_record = _load_json(
            _record_path(paths, str(ownership["transfer_id"])), required=True
        )
    receipt = _validate_tombstone_receipt(paths, tombstone, bound_record)
    if receipt["receipt_digest"] != ownership.get("receipt_digest"):
        raise TransferError("corrupt_ownership", "ownership receipt binding mismatch")
    if receipt["acknowledged_at"] != ownership.get("acknowledged_at"):
        raise TransferError("corrupt_ownership", "ownership acknowledgement time mismatch")
    if tombstone.get("revoked_at") != receipt["acknowledged_at"]:
        raise TransferError("corrupt_ownership", "revocation time does not match receipt")
    for key, value in _identity(bound_record).items():
        if ownership.get(key) != value:
            raise TransferError("ownership_conflict", f"ownership identity mismatch: {key}")


def _ownership_matches(
    paths: TransferPaths,
    ownership: Mapping[str, Any],
    record: Mapping[str, Any],
) -> bool:
    try:
        _validate_ownership(paths, ownership, record)
    except TransferError:
        return False
    return True


def _reconcile_ack_projection(
    paths: TransferPaths,
    record: dict[str, Any],
    ownership: Mapping[str, Any],
) -> None:
    if PHASE_INDEX.get(str(record.get("phase")), -1) < PHASE_INDEX["acknowledged"]:
        record["phase"] = "acknowledged"
        record["acknowledgement"] = {
            "result": "accepted",
            "acknowledged_at": ownership["acknowledged_at"],
            "receipt_digest": ownership["receipt_digest"],
            "ownership_epoch": ownership["ownership_epoch"],
        }
        record["failure"] = None
        _append_event(record, "acknowledged_reconciled")
        _write_record(paths, record)
        _fault("after_journal_before_pointer")
    _write_pointer(paths, record)


def acknowledge(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    destination_session_id: str,
    destination_task_id: str,
    goal_identity: str,
    capsule_path: str,
    capsule_revision: int,
    capsule_sha256: str,
    nonce: str,
) -> dict[str, Any]:
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    expected = _expected_identity(
        source_session_id=source_session_id,
        destination_session_id=destination_session_id,
        destination_task_id=destination_task_id,
        goal_identity=goal_identity,
        capsule_path=capsule_path,
        capsule_revision=capsule_revision,
        capsule_sha256=_validate_sha256(capsule_sha256),
        nonce=_validate_nonce(nonce),
    )
    with _locked(paths.lock):
        record = _active_record(paths)
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_acknowledgement", "transfer is no longer active")
        _validate_exact(record, expected)
        if record.get("verification", {}).get("result") != "verified":
            raise TransferError("verification_required", "exact destination verification is required")
        if not _capsule_matches(paths, record):
            raise TransferError("capsule_verification_failed", "capsule changed after verification")

        ownership = _load_json(paths.ownership)
        if ownership:
            ownership_paths = transfer_paths(
                repo, str(ownership.get("source_session_id", ""))
            )
            _validate_ownership(ownership_paths, ownership)
        if _ownership_matches(paths, ownership, record):
            _reconcile_ack_projection(paths, record, ownership)
            result = _result(record, idempotent=True)
            result["can_continue"] = _ownership_can_continue(ownership)
            return result
        previous_ownership = ownership
        if ownership and not (
            ownership.get("sole_writer_session_id") == source_session_id
            and _ownership_can_continue(ownership)
        ):
            raise TransferError("ownership_conflict", "ownership belongs to another transfer")

        tombstone = _load_json(paths.tombstone)
        if tombstone:
            if tombstone.get("transfer_id") != transfer_id or tombstone.get("nonce") != record.get("nonce"):
                raise TransferError("ownership_conflict", "source is revoked by another transfer")
            receipt = _validate_tombstone_receipt(paths, tombstone, record)
        else:
            receipt = _receipt(record, _now())
            tombstone = {
                "version": TRANSFER_VERSION,
                "source_session_id": source_session_id,
                "source_scope": paths.source_scope,
                "transfer_id": transfer_id,
                "nonce": record["nonce"],
                "revoked_at": receipt["acknowledged_at"],
                "enforced_read_only": True,
                "receipt": receipt,
            }
            tombstone = durable_write_json(paths.tombstone, tombstone)
        _fault("after_tombstone_before_ownership")

        ownership = {
            "version": TRANSFER_VERSION,
            "ownership_epoch": int(previous_ownership.get("ownership_epoch", 0)) + 1,
            "transfer_id": transfer_id,
            "source_session_id": source_session_id,
            "source_scope": paths.source_scope,
            "source_revoked": True,
            "destination_session_id": record["destination_session_id"],
            "destination_task_id": record["destination_task_id"],
            "sole_writer_session_id": record["destination_session_id"],
            "goal_identity": record["goal_identity"],
            "capsule_path": record["capsule_path"],
            "capsule_revision": record["capsule_revision"],
            "capsule_sha256": record["capsule_sha256"],
            "resume_ready": True,
            "nonce": record["nonce"],
            "acknowledged_at": receipt["acknowledged_at"],
            "receipt_digest": receipt["receipt_digest"],
            "tombstone_digest": _digest(tombstone),
            "ownership_committed_at": _now(),
            "source_stop": {
                "requested": False,
                "observed_quiesced": False,
                "termination_pending": False,
                "enforced_read_only": True,
            },
        }
        ownership = durable_write_json(paths.ownership, ownership)
        _fault("after_ownership_before_journal")
        _reconcile_ack_projection(paths, record, ownership)
        result = _result(record)
        result["ownership_epoch"] = ownership["ownership_epoch"]
        result["can_continue"] = False
        return result


def acknowledgement_timeout(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    detail: str = "acknowledgement deadline elapsed",
) -> dict[str, Any]:
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    with _locked(paths.lock):
        record = _active_record(paths)
        if record["transfer_id"] != transfer_id:
            raise TransferError("stale_transfer", "transfer is no longer active")
        if PHASE_INDEX[record["phase"]] >= PHASE_INDEX["acknowledged"]:
            return _result(record, idempotent=True)
        _failure(record, "acknowledgement_timed_out", detail)
        _write_record(paths, record)
        _write_pointer(paths, record)
        return _result(record)


def request_stop(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    capability: str,
) -> dict[str, Any]:
    if capability not in STOP_CAPABILITIES:
        raise TransferError("invalid_stop_capability", f"unsupported capability: {capability}")
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    with _locked(paths.lock):
        ownership = _load_json(paths.ownership, required=True)
        record = _record_from_ownership(paths, ownership, repair_projection=True)
        if record["transfer_id"] != transfer_id or not _ownership_matches(paths, ownership, record):
            raise TransferError("ownership_conflict", "stop requires acknowledged ownership")
        authoritative_stop = ownership.get("source_stop", {})
        if authoritative_stop.get("requested"):
            if authoritative_stop.get("capability") == capability:
                _reconcile_stop_projection(paths, record, ownership)
                return _result(record, idempotent=True)
            raise TransferError("stop_request_conflict", "stop already requested with another capability")
        stop = {
            "requested": True,
            "capability": capability,
            "requested_at": _now(),
            "result": "requested",
            "retry_count": 0,
            "retry_limit": 3,
            "observed_quiesced": False,
            "termination_pending": False,
            "enforced_read_only": True,
        }
        ownership["source_stop"] = stop
        ownership = durable_write_json(paths.ownership, ownership)
        _fault("after_stop_request_ownership_before_journal")
        _reconcile_stop_projection(paths, record, ownership)
        return _result(record)


def _reconcile_stop_projection(
    paths: TransferPaths,
    record: dict[str, Any],
    ownership: Mapping[str, Any],
) -> None:
    stop = ownership.get("source_stop")
    if not isinstance(stop, dict) or not stop.get("requested"):
        return
    changed = record.get("stop") != stop
    if stop.get("observed_quiesced"):
        changed = changed or record.get("phase") != "source_quiesced"
        record["phase"] = "source_quiesced"
        record["failure"] = None
        event = "source_quiesced"
    else:
        changed = changed or PHASE_INDEX.get(str(record.get("phase")), -1) < PHASE_INDEX[
            "source_stop_requested"
        ]
        record["phase"] = "source_stop_requested"
        failure_code = stop.get("failure_code")
        if failure_code:
            record["failure"] = {
                "code": failure_code,
                "detail": str(stop.get("detail", "")),
                "at": str(stop.get("observed_at", stop.get("requested_at", _now()))),
            }
        event = (
            "source_termination_pending"
            if stop.get("termination_pending")
            else "source_stop_requested"
        )
    record["stop"] = dict(stop)
    if changed:
        _append_event(record, event, result=stop.get("result"))
        _write_record(paths, record)
        _fault("after_stop_journal_before_pointer")
    _write_pointer(paths, record)


def _record_from_ownership(
    paths: TransferPaths,
    ownership: Mapping[str, Any],
    *,
    repair_projection: bool,
) -> dict[str, Any]:
    transfer_id = ownership.get("transfer_id")
    if not isinstance(transfer_id, str) or not transfer_id:
        raise TransferError("corrupt_ownership", "ownership transfer identity is missing")
    record = _load_json(_record_path(paths, transfer_id), required=True)
    _validate_ownership(paths, ownership, record)
    if repair_projection:
        _reconcile_ack_projection(paths, record, ownership)
        _reconcile_stop_projection(paths, record, ownership)
    return record


def record_stop(
    repo: Path,
    *,
    source_session_id: str,
    transfer_id: str,
    result: str,
    detail: str = "",
    evidence_kind: str = "",
    evidence_reference: str = "",
) -> dict[str, Any]:
    if result not in FINAL_STOP_RESULTS | PENDING_STOP_RESULTS:
        raise TransferError("invalid_stop_result", f"unsupported stop result: {result}")
    paths, _ = _load_exact(repo, source_session_id, transfer_id)
    with _locked(paths.lock):
        ownership = _load_json(paths.ownership, required=True)
        record = _record_from_ownership(paths, ownership, repair_projection=True)
        if record["transfer_id"] != transfer_id or not _ownership_matches(paths, ownership, record):
            raise TransferError("ownership_conflict", "stop result requires acknowledged ownership")
        authoritative_stop = ownership.get("source_stop")
        if not isinstance(authoritative_stop, dict) or not authoritative_stop.get("requested"):
            raise TransferError("invalid_transition", "stop must be requested first")
        capability = str(authoritative_stop.get("capability", ""))
        evidence: dict[str, Any] | None = None
        if result in FINAL_STOP_RESULTS:
            expected_kind = FINAL_STOP_EVIDENCE.get((capability, result))
            if expected_kind is None:
                raise TransferError(
                    "invalid_stop_evidence",
                    f"{capability or 'missing capability'} cannot establish {result}",
                )
            if evidence_kind.strip() != expected_kind or not evidence_reference.strip():
                raise TransferError(
                    "invalid_stop_evidence",
                    f"{result} requires {expected_kind} and a durable evidence reference",
                )
            evidence = {
                "kind": expected_kind,
                "reference": evidence_reference.strip(),
            }
        elif evidence_kind.strip() or evidence_reference.strip():
            raise TransferError(
                "invalid_stop_evidence",
                "pending stop outcomes must not assert adapter success evidence",
            )
        if authoritative_stop.get("result") == result and result in FINAL_STOP_RESULTS | PENDING_STOP_RESULTS:
            if result in FINAL_STOP_RESULTS:
                existing_evidence = authoritative_stop.get("adapter_evidence")
                if (
                    not isinstance(existing_evidence, dict)
                    or existing_evidence.get("kind") != evidence["kind"]
                    or existing_evidence.get("reference") != evidence["reference"]
                    or str(authoritative_stop.get("detail", "")) != detail
                ):
                    raise TransferError("stop_result_conflict", "stop success retry evidence differs")
            _reconcile_stop_projection(paths, record, ownership)
            response = _result(record, idempotent=True)
            response["can_continue"] = _ownership_can_continue(ownership)
            return response
        if authoritative_stop.get("result") in FINAL_STOP_RESULTS:
            raise TransferError("stop_result_conflict", "observed quiescence cannot be downgraded")
        stop = dict(authoritative_stop)
        stop["result"] = result
        stop["detail"] = detail
        stop["observed_at"] = _now()
        stop["enforced_read_only"] = True
        if result in FINAL_STOP_RESULTS:
            stop["observed_quiesced"] = True
            stop["termination_pending"] = False
            stop["failure_code"] = None
            stop["adapter_evidence"] = {
                **evidence,
                "recorded_at": stop["observed_at"],
            }
        else:
            stop["observed_quiesced"] = False
            stop["termination_pending"] = True
            stop["retry_count"] = min(int(stop.get("retry_count", 0)) + 1, int(stop["retry_limit"]))
            stop["retry_deadline"] = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
            ).isoformat(timespec="milliseconds")
            code = "stop_request_unsupported" if result == "unsupported" else "stop_request_failed"
            if result == "termination_pending":
                code = "source_termination_pending"
            stop["failure_code"] = code
        ownership["source_stop"] = dict(stop)
        ownership = durable_write_json(paths.ownership, ownership)
        _fault("after_stop_ownership_before_journal")
        _reconcile_stop_projection(paths, record, ownership)
        response = _result(record)
        response["can_continue"] = _ownership_can_continue(ownership)
        return response


def status(repo: Path, *, source_session_id: str) -> dict[str, Any]:
    paths = transfer_paths(repo, source_session_id)
    with _locked(paths.lock):
        ownership = _load_json(paths.ownership)
        if ownership:
            record = _record_from_ownership(paths, ownership, repair_projection=True)
        else:
            try:
                record = _active_record(paths)
            except TransferError as error:
                if error.code == "missing_state":
                    record = {}
                else:
                    raise
        tombstone = _load_json(paths.tombstone)
        response = {
            "ok": True,
            "source_session_id": source_session_id,
            "source_scope": paths.source_scope,
            "transfer": _identity(record) if record else None,
            "phase": record.get("phase") if record else None,
            "failure": record.get("failure") if record else None,
            "ownership": ownership or None,
            "source_revoked": bool(tombstone),
            "termination_pending": bool(
                isinstance(ownership.get("source_stop"), dict)
                and ownership["source_stop"].get("termination_pending")
            ),
            "can_continue": bool(_ownership_matches(paths, ownership, record) and _ownership_can_continue(ownership)),
            "process_group_interruption": dict(PROCESS_GROUP_INTERRUPTION),
        }
        return response


def _guard_write_locked(
    repo: Path,
    *,
    actor_session_id: str,
    source_session_id: str,
) -> dict[str, Any]:
    root = state_root(repo)
    actor_paths = transfer_paths(repo, actor_session_id)
    if actor_paths.tombstone.exists():
        return {"ok": True, "allowed": False, "reason": "actor_revoked", "can_continue": False}
    ownership_corrupt = False
    try:
        ownership = _load_json(root / ".ownership.json")
    except TransferError:
        ownership = {}
        ownership_corrupt = True
    if ownership:
        ownership_identities = {
            str(value)
            for value in (
                ownership.get("source_session_id"),
                ownership.get("destination_session_id"),
                ownership.get("sole_writer_session_id"),
            )
            if isinstance(value, str) and value
        }
        try:
            owner_paths = transfer_paths(repo, str(ownership.get("source_session_id", "")))
            _validate_ownership(owner_paths, ownership)
        except TransferError:
            if {actor_session_id, source_session_id}.intersection(ownership_identities):
                return {"ok": True, "allowed": False, "reason": "ownership_corrupt", "can_continue": False}
            ownership_corrupt = True
            ownership = {}
        else:
            if actor_session_id == ownership.get("source_session_id"):
                return {"ok": True, "allowed": False, "reason": "source_revoked", "can_continue": False}
            if actor_session_id == ownership.get("destination_session_id"):
                allowed = _ownership_can_continue(ownership)
                return {
                    "ok": True,
                    "allowed": allowed,
                    "reason": "destination_authorized" if allowed else "destination_embargoed",
                    "can_continue": allowed,
                }
            if source_session_id in ownership_identities:
                return {"ok": True, "allowed": False, "reason": "not_sole_writer", "can_continue": False}
    if source_session_id:
        paths = transfer_paths(repo, source_session_id)
        if paths.tombstone.exists():
            try:
                tombstone = _load_json(paths.tombstone, required=True)
                receipt = tombstone.get("receipt")
                destination = (
                    receipt.get("destination_session_id")
                    if isinstance(receipt, dict)
                    else None
                )
            except TransferError:
                return {"ok": True, "allowed": False, "reason": "transfer_state_corrupt", "can_continue": False}
            if actor_session_id == source_session_id:
                return {"ok": True, "allowed": False, "reason": "source_revoked", "can_continue": False}
            if actor_session_id == destination:
                return {"ok": True, "allowed": False, "reason": "destination_embargoed", "can_continue": False}
            return {"ok": True, "allowed": False, "reason": "not_source_authority", "can_continue": False}
        try:
            record = _active_record(paths)
        except TransferError as error:
            if error.code != "missing_state":
                return {"ok": True, "allowed": False, "reason": "transfer_state_corrupt", "can_continue": False}
            record = {}
        if record and actor_session_id == record.get("destination_session_id"):
            return {"ok": True, "allowed": False, "reason": "destination_embargoed", "can_continue": False}
        if record and actor_session_id != record.get("source_session_id"):
            return {"ok": True, "allowed": False, "reason": "not_source_authority", "can_continue": False}
    return {
        "ok": True,
        "allowed": True,
        "reason": "source_authority_unrelated_to_corrupt_ownership" if ownership_corrupt else "source_authority",
        "can_continue": True,
    }


@contextmanager
def authority_transaction(
    repo: Path,
    *,
    actor_session_id: str,
    source_session_id: str,
) -> Iterator[WriteFence]:
    """Hold the global transfer lock across an authorized state/filesystem write.

    Callers must perform the complete write before leaving this context.  A
    standalone ``guard-write`` result is advisory and cannot substitute for
    this epoch-fenced transaction because acknowledgement may race it.
    """

    actor_session_id = _require_text("actor session ID", actor_session_id)
    source_session_id = _require_text("source session ID", source_session_id)
    root = state_root(repo)
    with _locked(root / ".transfer.lock"):
        decision = _guard_write_locked(
            repo,
            actor_session_id=actor_session_id,
            source_session_id=source_session_id,
        )
        if not decision["allowed"]:
            raise TransferError("write_not_authorized", str(decision["reason"]))
        ownership = _load_json(root / ".ownership.json")
        fence = WriteFence(
            actor_session_id=actor_session_id,
            ownership_epoch=int(ownership.get("ownership_epoch", 0)),
            ownership_digest=_digest(ownership) if ownership else "implicit-source-authority",
            reason=str(decision["reason"]),
        )
        stack = list(getattr(_AUTHORITY_LOCAL, "stack", []))
        stack.append(fence)
        _AUTHORITY_LOCAL.stack = stack
        try:
            yield fence
        finally:
            stack.pop()
            _AUTHORITY_LOCAL.stack = stack


def require_authority_transaction() -> WriteFence:
    stack = getattr(_AUTHORITY_LOCAL, "stack", [])
    if not stack:
        raise TransferError(
            "write_not_fenced",
            "filesystem mutation requires a lock-held authority transaction",
        )
    return stack[-1]


def guard_write(
    repo: Path,
    *,
    actor_session_id: str,
    source_session_id: str,
) -> dict[str, Any]:
    actor_session_id = _require_text("actor session ID", actor_session_id)
    source_session_id = _require_text("source session ID", source_session_id)
    root = state_root(repo)
    with _locked(root / ".transfer.lock"):
        result = _guard_write_locked(
            repo,
            actor_session_id=actor_session_id,
            source_session_id=source_session_id,
        )
    result["advisory"] = True
    result["requires_authority_transaction"] = True
    return result


_HOOK_SESSION_FIELDS = (
    "session_id", "sessionId", "conversation_id", "conversationId",
    "composer_id", "composerId", "thread_id", "threadId",
)
_HOOK_READ_ONLY_TOOLS = {
    "read", "read_file", "glob", "grep", "search", "find", "list_files",
    "view_image", "read_mcp_resource", "get_goal", "read_goal",
    "get_goal_status", "read_thread",
}
_HOOK_CONTROL_TOOLS = {"bash", "shell", "exec_command"}
_HOOK_CONTROL_ACTIONS = {"verify", "acknowledge", "status", "request-stop", "record-stop"}
_HOOK_VALUE_OPTIONS = {
    "verify": {
        "--source-session-id", "--transfer-id", "--destination-session-id",
        "--destination-task-id", "--goal-identity", "--capsule-path",
        "--capsule-revision", "--capsule-sha256", "--nonce",
        "--exact-next-action", "--smallest-validation",
    },
    "acknowledge": {
        "--source-session-id", "--transfer-id", "--destination-session-id",
        "--destination-task-id", "--goal-identity", "--capsule-path",
        "--capsule-revision", "--capsule-sha256", "--nonce",
    },
    "status": {"--source-session-id"},
    "request-stop": {"--source-session-id", "--transfer-id", "--capability"},
    "record-stop": {"--source-session-id", "--transfer-id", "--result", "--detail"},
}
_HOOK_BOOLEAN_OPTIONS = {
    "verify": {"--repository-inspected", "--goal-inspected"},
    "acknowledge": set(), "status": set(), "request-stop": set(), "record-stop": set(),
}
_HOOK_METACHARACTERS = re.compile(r"[;&|`$()<>\\\n\r]")


def _hook_session(payload: Mapping[str, Any]) -> str | None:
    for key in _HOOK_SESSION_FIELDS:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    for key in ("context", "telemetry", "usage", "metrics", "session"):
        value = payload.get(key)
        if isinstance(value, dict):
            found = _hook_session(value)
            if found:
                return found
    return None


def discover_source_for_actor(repo: Path, actor_session_id: str) -> str | None:
    """Find an actor's source from ownership, active records, or durable receipts."""

    root = state_root(repo)
    matches: set[str] = set()
    try:
        ownership = _load_json(root / ".ownership.json")
    except TransferError:
        ownership = {}
    if ownership:
        source = ownership.get("source_session_id")
        try:
            owner_paths = transfer_paths(repo, str(source or ""))
            _validate_ownership(owner_paths, ownership)
        except TransferError:
            pass
        else:
            if actor_session_id in {
                ownership.get("source_session_id"), ownership.get("destination_session_id")
            } and isinstance(source, str) and source:
                return source
    for pointer_path in (root / "sessions").glob("*/.active-transfer.json"):
        try:
            pointer = _load_json(pointer_path, required=True)
        except TransferError:
            continue
        if pointer.get("destination_session_id") == actor_session_id:
            source = pointer.get("source_session_id")
            if isinstance(source, str) and source:
                matches.add(source)
    for record_path in (root / "sessions").glob("*/transfers/*.json"):
        try:
            record = _load_json(record_path, required=True)
            source = record.get("source_session_id")
            if not isinstance(source, str) or not source:
                continue
            record_paths = transfer_paths(repo, source)
            _validate_record(record_paths, record)
        except TransferError:
            continue
        if record.get("destination_session_id") == actor_session_id:
            matches.add(source)
    for tombstone_path in (root / "sessions").glob("*/.revoked.json"):
        try:
            tombstone = _load_json(tombstone_path, required=True)
        except TransferError:
            continue
        receipt = tombstone.get("receipt")
        if isinstance(receipt, dict) and receipt.get("destination_session_id") == actor_session_id:
            source = tombstone.get("source_session_id") or receipt.get("source_session_id")
            if isinstance(source, str) and source:
                matches.add(source)
    if len(matches) > 1:
        raise TransferError("ambiguous_binding", "actor is bound to multiple source transfers")
    return next(iter(matches), None)


def _control_binding(
    repo: Path,
    *,
    actor_session_id: str,
    source_session_id: str,
) -> dict[str, Any]:
    paths = transfer_paths(repo, source_session_id)
    records: list[dict[str, Any]] = []
    try:
        records.append(_active_record(paths))
    except TransferError:
        pass
    try:
        tombstone = _load_json(paths.tombstone)
    except TransferError:
        tombstone = {}
    if tombstone:
        transfer_id = tombstone.get("transfer_id")
        if isinstance(transfer_id, str) and transfer_id:
            try:
                record = _load_json(_record_path(paths, transfer_id), required=True)
                _validate_tombstone_receipt(paths, tombstone, record)
                records.append(record)
            except TransferError:
                pass
    identities = {
        _digest(_identity(record)): _identity(record)
        for record in records
        if record.get("destination_session_id") == actor_session_id
    }
    if len(identities) != 1:
        raise TransferError("control_binding_unavailable", "exact active transfer binding is unavailable")
    return next(iter(identities.values()))


def _parse_bound_control_command(
    repo: Path,
    command: str,
    binding: Mapping[str, Any],
) -> bool:
    if not command or _HOOK_METACHARACTERS.search(command):
        return False
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return False
    if len(words) < 5:
        return False
    script = Path(__file__).resolve()
    if Path(words[0]).name in {"python", "python3", "python3.14"}:
        raw_interpreter = words[0]
        resolved_interpreter = shutil.which(raw_interpreter) if "/" not in raw_interpreter else raw_interpreter
        if not resolved_interpreter:
            return False
        try:
            interpreter = Path(resolved_interpreter).expanduser().resolve(strict=True)
            trusted = {Path(sys.executable).resolve(strict=True)}
            path_python = shutil.which("python3")
            if path_python:
                trusted.add(Path(path_python).resolve(strict=True))
        except OSError:
            return False
        if interpreter not in trusted:
            return False
        script_index = 1
    else:
        script_index = 0
    try:
        if Path(words[script_index]).expanduser().resolve(strict=True) != script:
            return False
    except OSError:
        return False
    arguments = words[script_index + 1 :]
    if len(arguments) < 3 or arguments[0] != "--repo":
        return False
    try:
        if Path(arguments[1]).expanduser().resolve() != repo.resolve():
            return False
    except OSError:
        return False
    arguments = arguments[2:]
    if not arguments or arguments[0] not in _HOOK_CONTROL_ACTIONS:
        return False
    action = arguments[0]
    values: dict[str, str] = {}
    booleans: set[str] = set()
    index = 1
    while index < len(arguments):
        option = arguments[index]
        if option in values or option in booleans:
            return False
        if option in _HOOK_BOOLEAN_OPTIONS[action]:
            booleans.add(option)
            index += 1
            continue
        if option not in _HOOK_VALUE_OPTIONS[action] or index + 1 >= len(arguments):
            return False
        value = arguments[index + 1]
        if not value or value.startswith("--"):
            return False
        values[option] = value
        index += 2
    required_values = _HOOK_VALUE_OPTIONS[action] - ({"--detail"} if action == "record-stop" else set())
    if not required_values.issubset(values) or not _HOOK_BOOLEAN_OPTIONS[action].issubset(booleans):
        return False
    if action == "record-stop" and values.get("--result") not in PENDING_STOP_RESULTS:
        return False
    expected = {
        "--source-session-id": str(binding.get("source_session_id", "")),
        "--transfer-id": str(binding.get("transfer_id", "")),
        "--destination-session-id": str(binding.get("destination_session_id", "")),
        "--destination-task-id": str(binding.get("destination_task_id", "")),
        "--goal-identity": str(binding.get("goal_identity", "")),
        "--capsule-revision": str(binding.get("capsule_revision", "")),
        "--capsule-sha256": str(binding.get("capsule_sha256", "")),
        "--nonce": str(binding.get("nonce", "")),
    }
    for option, expected_value in expected.items():
        if option in values and values[option] != expected_value:
            return False
    if "--capsule-path" in values:
        try:
            if Path(values["--capsule-path"]).expanduser().resolve() != Path(str(binding["capsule_path"])).resolve():
                return False
        except (KeyError, OSError):
            return False
    return True


def _parse_readonly_repo_command(repo: Path, command: str) -> bool:
    if not command or _HOOK_METACHARACTERS.search(command):
        return False
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return False
    allowed = {
        ("pwd",),
        ("git", "status", "--short"),
        ("git", "diff", "--stat"),
        ("git", "diff", "--name-only"),
        ("git", "rev-parse", "--show-toplevel"),
    }
    if tuple(words) not in allowed:
        return False
    if words == ["pwd"]:
        return True
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(repo),
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and Path(completed.stdout.strip()).resolve() == repo.resolve()


def hook_pretool_decision(repo: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    actor = _hook_session(payload) or "default"
    source = discover_source_for_actor(repo, actor) or actor
    decision = guard_write(repo, actor_session_id=actor, source_session_id=source)
    if decision["allowed"]:
        return {"continue": True}
    reason = str(decision["reason"])
    raw_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(raw_input, dict):
        raw_input = {}
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or "").strip().lower()
    command = str(raw_input.get("command") or raw_input.get("cmd") or "").strip()
    if reason == "destination_embargoed":
        if tool_name in _HOOK_READ_ONLY_TOOLS:
            return {"continue": True}
        if tool_name in _HOOK_CONTROL_TOOLS:
            try:
                binding = _control_binding(
                    repo,
                    actor_session_id=actor,
                    source_session_id=source,
                )
            except TransferError:
                binding = {}
            if binding and _parse_bound_control_command(repo, command, binding):
                return {"continue": True}
            workdir = raw_input.get("workdir")
            try:
                workdir_matches = (
                    workdir is None
                    or Path(str(workdir)).expanduser().resolve() == repo.resolve()
                )
            except OSError:
                workdir_matches = False
            if workdir_matches and _parse_readonly_repo_command(repo, command):
                return {"continue": True}
    return {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def _result(record: Mapping[str, Any], *, idempotent: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "idempotent": idempotent,
        "transfer_id": record.get("transfer_id"),
        "phase": record.get("phase"),
        "source_session_id": record.get("source_session_id"),
        "destination_session_id": record.get("destination_session_id"),
        "destination_task_id": record.get("destination_task_id"),
        "goal_identity": record.get("goal_identity"),
        "capsule_path": record.get("capsule_path"),
        "capsule_revision": record.get("capsule_revision"),
        "capsule_sha256": record.get("capsule_sha256"),
        "nonce": record.get("nonce"),
        "failure": record.get("failure"),
        "can_continue": False,
        "process_group_interruption": dict(PROCESS_GROUP_INTERRUPTION),
    }


def _add_transfer(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-session-id", required=True)
    parser.add_argument("--transfer-id", required=True)


def _add_exact(parser: argparse.ArgumentParser) -> None:
    _add_transfer(parser)
    parser.add_argument("--destination-session-id", required=True)
    parser.add_argument("--destination-task-id", required=True)
    parser.add_argument("--goal-identity", required=True)
    parser.add_argument("--capsule-path", required=True)
    parser.add_argument("--capsule-revision", required=True, type=int)
    parser.add_argument("--capsule-sha256", required=True)
    parser.add_argument("--nonce", required=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    subparsers = parser.add_subparsers(dest="command", required=True)

    command = subparsers.add_parser("prepare")
    command.add_argument("--source-session-id", required=True)
    command.add_argument("--goal-identity", required=True)
    command.add_argument("--capsule-path", required=True)
    command.add_argument("--capsule-revision", required=True, type=int)
    command.add_argument("--capsule-sha256", required=True)
    command.add_argument("--resume-ready", action="store_true")
    command.add_argument("--nonce", default="")

    command = subparsers.add_parser("launch-requested")
    _add_transfer(command)
    command.add_argument("--transport-key", required=True)

    command = subparsers.add_parser("launch-outcome")
    _add_transfer(command)
    command.add_argument("--outcome", choices=("failed", "unknown"), required=True)
    command.add_argument("--detail", default="")

    command = subparsers.add_parser("delivered")
    _add_transfer(command)
    command.add_argument("--transport-key", required=True)
    command.add_argument("--destination-task-id", required=True)

    command = subparsers.add_parser("started")
    _add_transfer(command)
    command.add_argument("--destination-session-id", required=True)
    command.add_argument("--destination-task-id", required=True)

    command = subparsers.add_parser("reconcile-launch")
    _add_transfer(command)
    command.add_argument("--transport-key", required=True)
    command.add_argument("--observed-nonce", required=True)
    command.add_argument("--destination-session-id", required=True)
    command.add_argument("--destination-task-id", required=True)

    command = subparsers.add_parser("verify")
    _add_exact(command)
    command.add_argument("--repository-inspected", action="store_true")
    command.add_argument("--goal-inspected", action="store_true")
    command.add_argument("--exact-next-action", required=True)
    command.add_argument("--smallest-validation", required=True)

    command = subparsers.add_parser("acknowledge")
    _add_exact(command)

    command = subparsers.add_parser("ack-timeout")
    _add_transfer(command)
    command.add_argument("--detail", default="acknowledgement deadline elapsed")

    command = subparsers.add_parser("request-stop")
    _add_transfer(command)
    command.add_argument("--capability", choices=tuple(sorted(STOP_CAPABILITIES)), required=True)

    command = subparsers.add_parser("record-stop")
    _add_transfer(command)
    command.add_argument("--result", choices=tuple(sorted(FINAL_STOP_RESULTS | PENDING_STOP_RESULTS)), required=True)
    command.add_argument("--detail", default="")
    command.add_argument("--evidence-kind", default="")
    command.add_argument("--evidence-reference", default="")

    command = subparsers.add_parser("status")
    command.add_argument("--source-session-id", required=True)

    command = subparsers.add_parser("guard-write")
    command.add_argument("--actor-session-id", required=True)
    command.add_argument("--source-session-id", required=True)

    subparsers.add_parser("hook-pretool")
    return parser.parse_args(argv)


def _exact_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source_session_id": args.source_session_id,
        "transfer_id": args.transfer_id,
        "destination_session_id": args.destination_session_id,
        "destination_task_id": args.destination_task_id,
        "goal_identity": args.goal_identity,
        "capsule_path": args.capsule_path,
        "capsule_revision": args.capsule_revision,
        "capsule_sha256": args.capsule_sha256,
        "nonce": args.nonce,
    }


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    command = args.command
    if command == "prepare":
        return prepare(
            repo,
            source_session_id=args.source_session_id,
            goal_identity=args.goal_identity,
            capsule_path=args.capsule_path,
            capsule_revision=args.capsule_revision,
            capsule_sha256=args.capsule_sha256,
            resume_ready=args.resume_ready,
            nonce=args.nonce,
        )
    if command == "launch-requested":
        return launch_requested(repo, source_session_id=args.source_session_id, transfer_id=args.transfer_id, transport_key=args.transport_key)
    if command == "launch-outcome":
        return record_launch_outcome(repo, source_session_id=args.source_session_id, transfer_id=args.transfer_id, outcome=args.outcome, detail=args.detail)
    if command == "delivered":
        return delivered(repo, source_session_id=args.source_session_id, transfer_id=args.transfer_id, transport_key=args.transport_key, destination_task_id=args.destination_task_id)
    if command == "started":
        return started(repo, source_session_id=args.source_session_id, transfer_id=args.transfer_id, destination_session_id=args.destination_session_id, destination_task_id=args.destination_task_id)
    if command == "reconcile-launch":
        return reconcile_launch(repo, source_session_id=args.source_session_id, transfer_id=args.transfer_id, transport_key=args.transport_key, observed_nonce=args.observed_nonce, destination_session_id=args.destination_session_id, destination_task_id=args.destination_task_id)
    if command == "verify":
        return verify(repo, **_exact_kwargs(args), repository_inspected=args.repository_inspected, goal_inspected=args.goal_inspected, exact_next_action=args.exact_next_action, smallest_validation=args.smallest_validation)
    if command == "acknowledge":
        return acknowledge(repo, **_exact_kwargs(args))
    if command == "ack-timeout":
        return acknowledgement_timeout(repo, source_session_id=args.source_session_id, transfer_id=args.transfer_id, detail=args.detail)
    if command == "request-stop":
        return request_stop(repo, source_session_id=args.source_session_id, transfer_id=args.transfer_id, capability=args.capability)
    if command == "record-stop":
        return record_stop(
            repo,
            source_session_id=args.source_session_id,
            transfer_id=args.transfer_id,
            result=args.result,
            detail=args.detail,
            evidence_kind=args.evidence_kind,
            evidence_reference=args.evidence_reference,
        )
    if command == "status":
        return status(repo, source_session_id=args.source_session_id)
    if command == "guard-write":
        return guard_write(repo, actor_session_id=args.actor_session_id, source_session_id=args.source_session_id)
    if command == "hook-pretool":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as error:
            raise TransferError("invalid_hook_payload", str(error)) from error
        if not isinstance(payload, dict):
            raise TransferError("invalid_hook_payload", "hook payload must be an object")
        return hook_pretool_decision(repo, payload)
    raise AssertionError(f"unhandled command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = dispatch(parse_args(argv))
    except (TransferError, FaultInjected) as error:
        code = error.code if isinstance(error, TransferError) else "fault_injected"
        print(json.dumps({"ok": False, "error": {"code": code, "message": str(error)}}))
        return 2
    except OSError as error:
        print(json.dumps({"ok": False, "error": {"code": "io_error", "message": str(error)}}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
