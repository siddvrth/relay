#!/usr/bin/env python3
"""Parse the independent Relay release manifest boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Final, TypeAlias


COMMIT_PATTERN: Final = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
VERSION_PATTERN: Final = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
FORBIDDEN_ROOTS: Final = frozenset({".agents", ".omo", ".omx", "private"})
FORBIDDEN_NAMES: Final = frozenset({".DS_Store", ".env", "__pycache__"})
FORBIDDEN_SUFFIXES: Final = (".jsonl", ".log", ".pyc", ".pyo", ".temp", ".tmp", "~")
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class ManifestFile:
    path: str
    mode: int
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ManifestArchive:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    source_commit: str
    version: str
    archive: ManifestArchive
    files: tuple[ManifestFile, ...]


@dataclass(frozen=True, slots=True)
class ManifestError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def _reject_duplicate_key(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    document: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in document:
            raise ManifestError(reason=f"duplicate manifest key: {key}")
        document[key] = value
    return document


def _mapping(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ManifestError(reason=f"{label} must be an object")
    return value


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(reason=f"{label} must be text")
    return value


def _integer(value: JsonValue, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(reason=f"{label} must be a non-negative integer")
    return value


def _sha256(value: JsonValue, label: str) -> str:
    digest = _text(value, label)
    if SHA256_PATTERN.fullmatch(digest) is None:
        raise ManifestError(reason=f"{label} must be 64 lowercase hex characters")
    return digest


def safe_release_path(value: JsonValue, label: str) -> str:
    raw = _text(value, label)
    path = PurePosixPath(raw)
    if (
        not raw
        or "\x00" in raw
        or "\\" in raw
        or raw.endswith("/")
        or path.is_absolute()
        or path.as_posix() != raw
        or unicodedata.normalize("NFC", raw) != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ManifestError(reason=f"{label} is unsafe: {raw!r}")
    if path.parts[0] in FORBIDDEN_ROOTS:
        raise ManifestError(reason=f"{label} contains local state: {raw!r}")
    if any(part in FORBIDDEN_NAMES for part in path.parts):
        raise ManifestError(reason=f"{label} contains generated state: {raw!r}")
    if raw.endswith(FORBIDDEN_SUFFIXES):
        raise ManifestError(reason=f"{label} contains temporary state: {raw!r}")
    return raw


def _file(value: JsonValue, index: int) -> ManifestFile:
    document = _mapping(value, f"files[{index}]")
    if set(document) != {"mode", "path", "sha256", "size"}:
        raise ManifestError(reason=f"files[{index}] has invalid fields")
    path = safe_release_path(document["path"], f"files[{index}].path")
    raw_mode = _text(document["mode"], f"files[{index}].mode")
    if raw_mode not in {"0644", "0755"}:
        raise ManifestError(reason=f"files[{index}].mode is invalid")
    return ManifestFile(
        path=path,
        mode=int(raw_mode, 8),
        sha256=_sha256(document["sha256"], f"files[{index}].sha256"),
        size=_integer(document["size"], f"files[{index}].size"),
    )


def parse_release_manifest(payload: bytes) -> ReleaseManifest:
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_key,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManifestError(reason=f"non-finite manifest number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(reason=f"malformed manifest: {error}") from error
    root = _mapping(document, "manifest")
    if set(root) != {"archive", "files", "schema_version", "source_commit", "version"}:
        raise ManifestError(reason="manifest has invalid fields")
    if root["schema_version"] != 1 or isinstance(root["schema_version"], bool):
        raise ManifestError(reason="schema_version must be 1")
    commit = _text(root["source_commit"], "source_commit")
    version = _text(root["version"], "version")
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise ManifestError(reason="source_commit must be a full lowercase Git SHA")
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ManifestError(reason="version must be semver")
    archive_document = _mapping(root["archive"], "archive")
    if set(archive_document) != {"path", "sha256", "size"}:
        raise ManifestError(reason="archive has invalid fields")
    archive_path = _text(archive_document["path"], "archive.path")
    expected_archive = f"relay-{version}-{commit}.tar.gz"
    if PurePosixPath(archive_path).name != archive_path or archive_path != expected_archive:
        raise ManifestError(reason="archive.path is not the canonical release name")
    raw_files = root["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise ManifestError(reason="files must be a non-empty array")
    files = tuple(_file(value, index) for index, value in enumerate(raw_files))
    paths = tuple(item.path for item in files)
    if paths != tuple(sorted(paths)):
        raise ManifestError(reason="manifest files must be sorted")
    if len(paths) != len(set(paths)):
        raise ManifestError(reason="manifest file paths must be unique")
    ambiguous = tuple(unicodedata.normalize("NFC", path).casefold() for path in paths)
    if len(ambiguous) != len(set(ambiguous)):
        raise ManifestError(reason="manifest file paths are ambiguous")
    canonical = (json.dumps(root, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if payload != canonical:
        raise ManifestError(reason="manifest bytes are not canonical JSON")
    return ReleaseManifest(
        source_commit=commit,
        version=version,
        archive=ManifestArchive(
            path=archive_path,
            sha256=_sha256(archive_document["sha256"], "archive.sha256"),
            size=_integer(archive_document["size"], "archive.size"),
        ),
        files=files,
    )
