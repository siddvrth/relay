#!/usr/bin/env python3
"""Independently verify a Relay release archive and sibling manifest."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
from typing import Final
import unicodedata
import zlib

from release_manifest import (
    COMMIT_PATTERN,
    JsonValue,
    ManifestError,
    ManifestFile,
    ReleaseManifest,
    parse_release_manifest,
    safe_release_path,
)


CONTRACT_PATH: Final = ".codex-plugin/release-files.json"
PLUGIN_PATH: Final = ".codex-plugin/plugin.json"
GZIP_HEADER: Final = bytes.fromhex("1f8b08000000000002ff")
ALLOWED_TAR_TYPES: Final = frozenset({b"\0", b"0", b"5"})


@dataclass(frozen=True, slots=True)
class VerifiedFile:
    path: str
    mode: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    source_commit: str
    version: str
    root: str
    files: tuple[VerifiedFile, ...]


@dataclass(frozen=True, slots=True)
class VerificationError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def _reject_duplicate_embedded_key(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    document: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in document:
            raise VerificationError(reason=f"duplicate embedded JSON key: {key}")
        document[key] = value
    return document


def _read_regular_file(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(reason=f"{label} must be a regular file")
    try:
        return path.read_bytes()
    except OSError as error:
        raise VerificationError(reason=f"cannot read {label}: {error}") from error


def _strict_gzip(payload: bytes) -> bytes:
    if not payload.startswith(GZIP_HEADER):
        raise VerificationError(reason="archive has noncanonical gzip metadata")
    decoder = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        expanded = decoder.decompress(payload) + decoder.flush()
    except zlib.error as error:
        raise VerificationError(reason=f"archive gzip is malformed: {error}") from error
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise VerificationError(reason="archive contains trailing or concatenated gzip data")
    return expanded


def _tar_size(field: bytes) -> int:
    encoded = field.rstrip(b"\0 ")
    try:
        return int(encoded or b"0", 8)
    except ValueError as error:
        raise VerificationError(reason="archive contains an invalid USTAR size") from error


def _guard_raw_ustar(payload: bytes) -> None:
    if len(payload) % tarfile.BLOCKSIZE != 0:
        raise VerificationError(reason="archive tar length is not block aligned")
    offset = 0
    while offset < len(payload):
        header = payload[offset : offset + tarfile.BLOCKSIZE]
        if header == b"\0" * tarfile.BLOCKSIZE:
            tail = payload[offset:]
            if len(tail) < tarfile.BLOCKSIZE * 2 or tail.strip(b"\0"):
                raise VerificationError(reason="archive has noncanonical tar termination")
            return
        if header[156:157] not in ALLOWED_TAR_TYPES:
            raise VerificationError(reason="archive contains links, special files, or extensions")
        size = _tar_size(header[124:136])
        offset += tarfile.BLOCKSIZE + ((size + 511) // 512) * 512
    raise VerificationError(reason="archive is missing the USTAR end marker")


def _directories(paths: tuple[str, ...]) -> tuple[str, ...]:
    directories: set[str] = set()
    for raw in paths:
        parent = PurePosixPath(raw).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return tuple(sorted(directories))


def _member_name(raw: str, root: str) -> str:
    if raw == root:
        return ""
    prefix = f"{root}/"
    if not raw.startswith(prefix):
        raise VerificationError(reason=f"archive member is outside the release root: {raw!r}")
    try:
        return safe_release_path(raw[len(prefix) :], "archive member")
    except ManifestError as error:
        raise VerificationError(reason=str(error)) from error


def _parse_contract(payload: bytes) -> tuple[str, ...]:
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_embedded_key)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError(reason=f"embedded release contract is malformed: {error}") from error
    if not isinstance(document, dict) or set(document) != {"paths", "schema_version"}:
        raise VerificationError(reason="embedded release contract has invalid fields")
    if document["schema_version"] != 1 or isinstance(document["schema_version"], bool):
        raise VerificationError(reason="embedded release contract has invalid schema")
    raw_paths = document["paths"]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise VerificationError(reason="embedded release contract paths are invalid")
    try:
        paths = tuple(safe_release_path(path, "release contract path") for path in raw_paths)
    except ManifestError as error:
        raise VerificationError(reason=str(error)) from error
    if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
        raise VerificationError(reason="embedded release contract paths are not exact")
    if CONTRACT_PATH not in paths:
        raise VerificationError(reason="embedded release contract does not include itself")
    return paths


def _verify_plugin(payload: bytes, version: str) -> None:
    try:
        document = json.loads(payload, object_pairs_hook=_reject_duplicate_embedded_key)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError(reason=f"embedded plugin manifest is malformed: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("name") != "relay"
        or document.get("version") != version
    ):
        raise VerificationError(reason="embedded plugin identity does not match the release")


def _verify_members(tar_payload: bytes, manifest: ReleaseManifest) -> VerifiedRelease:
    root = f"relay-{manifest.version}"
    paths = tuple(item.path for item in manifest.files)
    expected_names = (
        root,
        *(f"{root}/{directory}" for directory in _directories(paths)),
        *(f"{root}/{path}" for path in paths),
    )
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_payload), mode="r:", format=tarfile.USTAR_FORMAT) as bundle:
            members = bundle.getmembers()
            names = tuple(member.name for member in members)
            if names != expected_names:
                raise VerificationError(reason="archive membership or order does not match manifest")
            ambiguous = tuple(unicodedata.normalize("NFC", name).casefold() for name in names)
            if len(ambiguous) != len(set(ambiguous)):
                raise VerificationError(reason="archive contains duplicate or ambiguous members")
            file_offset = 1 + len(_directories(paths))
            verified: list[VerifiedFile] = []
            for index, member in enumerate(members):
                relative = _member_name(member.name, root)
                canonical_header = member.tobuf(
                    format=tarfile.USTAR_FORMAT,
                    encoding="utf-8",
                    errors="strict",
                )[: tarfile.BLOCKSIZE]
                raw_header = tar_payload[member.offset : member.offset + tarfile.BLOCKSIZE]
                if raw_header != canonical_header:
                    raise VerificationError(reason=f"archive header is noncanonical: {member.name}")
                if (
                    member.uid != 0
                    or member.gid != 0
                    or member.uname
                    or member.gname
                    or member.mtime != 0
                    or member.pax_headers
                ):
                    raise VerificationError(reason=f"archive metadata is noncanonical: {member.name}")
                if index < file_offset:
                    if not member.isdir() or member.mode != 0o755 or member.size != 0:
                        raise VerificationError(reason=f"archive directory is invalid: {member.name}")
                    continue
                expected = manifest.files[index - file_offset]
                if not member.isfile() or member.mode != expected.mode or relative != expected.path:
                    raise VerificationError(reason=f"archive file metadata is invalid: {member.name}")
                extracted = bundle.extractfile(member)
                if extracted is None:
                    raise VerificationError(reason=f"archive file cannot be read: {member.name}")
                payload = extracted.read()
                if len(payload) != expected.size:
                    raise VerificationError(reason=f"archive file size mismatch: {expected.path}")
                if hashlib.sha256(payload).hexdigest() != expected.sha256:
                    raise VerificationError(reason=f"archive file hash mismatch: {expected.path}")
                verified.append(VerifiedFile(expected.path, expected.mode, payload))
    except (tarfile.TarError, UnicodeError, ValueError) as error:
        raise VerificationError(reason=f"archive tar is malformed: {error}") from error
    payload_by_path = {item.path: item.payload for item in verified}
    if CONTRACT_PATH not in payload_by_path or PLUGIN_PATH not in payload_by_path:
        raise VerificationError(reason="release identity files are missing")
    contract = _parse_contract(payload_by_path[CONTRACT_PATH])
    if contract != paths:
        raise VerificationError(reason="manifest membership does not match embedded release contract")
    _verify_plugin(payload_by_path[PLUGIN_PATH], manifest.version)
    return VerifiedRelease(manifest.source_commit, manifest.version, root, tuple(verified))


def verify_release(
    archive_path: Path,
    manifest_path: Path,
    expected_commit: str | None = None,
) -> VerifiedRelease:
    archive = _read_regular_file(archive_path, "archive")
    manifest_payload = _read_regular_file(manifest_path, "manifest")
    try:
        manifest = parse_release_manifest(manifest_payload)
    except ManifestError as error:
        raise VerificationError(reason=str(error)) from error
    expected_manifest = manifest.archive.path.removesuffix(".tar.gz") + ".manifest.json"
    if archive_path.name != manifest.archive.path or manifest_path.name != expected_manifest:
        raise VerificationError(reason="artifact filenames do not match manifest metadata")
    if expected_commit is not None:
        if COMMIT_PATTERN.fullmatch(expected_commit) is None:
            raise VerificationError(reason="expected commit must be a full lowercase Git SHA")
        if manifest.source_commit != expected_commit:
            raise VerificationError(reason="release source commit does not match expected commit")
    if len(archive) != manifest.archive.size:
        raise VerificationError(reason="archive size does not match manifest")
    if hashlib.sha256(archive).hexdigest() != manifest.archive.sha256:
        raise VerificationError(reason="archive hash does not match manifest")
    tar_payload = _strict_gzip(archive)
    _guard_raw_ustar(tar_payload)
    return _verify_members(tar_payload, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-commit")
    args = parser.parse_args()
    try:
        verify_release(args.archive, args.manifest, args.expected_commit)
    except (OSError, VerificationError) as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        return 1
    print("release verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
