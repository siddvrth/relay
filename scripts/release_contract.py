#!/usr/bin/env python3
"""Parse Relay's exact public release file contract."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Final


CONTRACT_PATH: Final = ".codex-plugin/release-files.json"
FORBIDDEN_ROOTS: Final = frozenset({".agents", ".omo", ".omx", "private"})
FORBIDDEN_NAMES: Final = frozenset({".DS_Store", ".env", "__pycache__"})
FORBIDDEN_SUFFIXES: Final = (".jsonl", ".log", ".pyc", ".pyo", ".temp", ".tmp", "~")


@dataclass(frozen=True, slots=True)
class ReleaseContract:
    """Validated exact payload membership."""

    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReleaseContractError(ValueError):
    reason: str

    def __str__(self) -> str:
        return f"release contract: {self.reason}"


def _reject_duplicate_key(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ReleaseContractError(reason=f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _validate_path(raw: str) -> str:
    if "\x00" in raw or "\\" in raw:
        raise ReleaseContractError(reason=f"unsafe path {raw!r}")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReleaseContractError(reason=f"unsafe path {raw!r}")
    if path.parts[0] in FORBIDDEN_ROOTS:
        raise ReleaseContractError(reason=f"forbidden local state path {raw!r}")
    if any(part in FORBIDDEN_NAMES for part in path.parts):
        raise ReleaseContractError(reason=f"forbidden generated path {raw!r}")
    if raw.endswith(FORBIDDEN_SUFFIXES):
        raise ReleaseContractError(reason=f"forbidden temporary or log path {raw!r}")
    return raw


def parse_release_contract(payload: bytes) -> ReleaseContract:
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_key)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseContractError(reason=f"invalid JSON: {error}") from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "paths"}:
        raise ReleaseContractError(reason="expected only schema_version and paths")
    if document["schema_version"] != 1 or isinstance(document["schema_version"], bool):
        raise ReleaseContractError(reason="schema_version must be 1")
    raw_paths = document["paths"]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ReleaseContractError(reason="paths must be a non-empty array")
    if not all(isinstance(path, str) for path in raw_paths):
        raise ReleaseContractError(reason="every path must be text")
    paths = tuple(_validate_path(path) for path in raw_paths)
    if paths != tuple(sorted(paths)):
        raise ReleaseContractError(reason="paths must be sorted")
    if len(paths) != len(set(paths)):
        raise ReleaseContractError(reason="paths must be unique")
    if CONTRACT_PATH not in paths:
        raise ReleaseContractError(reason=f"{CONTRACT_PATH} must include itself")
    return ReleaseContract(paths=paths)


def validate_release_worktree(root: Path) -> ReleaseContract:
    contract_path = root / CONTRACT_PATH
    try:
        contract = parse_release_contract(contract_path.read_bytes())
    except OSError as error:
        raise ReleaseContractError(reason=f"cannot read {CONTRACT_PATH}: {error}") from error
    for relative in contract.paths:
        candidate = root / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ReleaseContractError(reason=f"listed path is not a regular file: {relative}")
    return contract
