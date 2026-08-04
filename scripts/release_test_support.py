#!/usr/bin/env python3
"""Shared real-artifact fixtures for release verifier tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import gzip
import io
from pathlib import Path
import tarfile
from typing import NoReturn, TypeVar

try:
    from typing import assert_never
except ImportError:
    _UnreachableValue = TypeVar("_UnreachableValue")

    def assert_never(value: _UnreachableValue) -> NoReturn:
        """Raise when a supposedly exhaustive match receives an unknown value."""
        raise AssertionError(f"Unhandled match value: {value!r}")

from test_build_release import artifacts, build, write_fixture


class EntryKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"


@dataclass(frozen=True, slots=True)
class TarEntry:
    name: str
    kind: EntryKind
    mode: int
    payload: bytes = b""
    linkname: str = ""


@dataclass(frozen=True, slots=True)
class BuiltArtifacts:
    archive: Path
    manifest: Path


@dataclass(frozen=True, slots=True)
class FixtureError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def build_valid_artifacts(root: Path) -> BuiltArtifacts:
    repo = root / "repo"
    output = root / "output"
    repo.mkdir()
    output.mkdir()
    commit = write_fixture(repo)
    result = build(repo, commit, output)
    if result.returncode != 0:
        raise FixtureError(reason=result.stderr)
    archive, manifest = artifacts(output)
    return BuiltArtifacts(archive=archive, manifest=manifest)


def read_entries(archive: Path) -> tuple[TarEntry, ...]:
    entries: list[TarEntry] = []
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            match member.type:
                case tarfile.REGTYPE | tarfile.AREGTYPE:
                    extracted = bundle.extractfile(member)
                    if extracted is None:
                        raise FixtureError(reason=f"missing payload for {member.name}")
                    entries.append(
                        TarEntry(
                            name=member.name,
                            kind=EntryKind.FILE,
                            mode=member.mode,
                            payload=extracted.read(),
                        )
                    )
                case tarfile.DIRTYPE:
                    entries.append(
                        TarEntry(
                            name=member.name,
                            kind=EntryKind.DIRECTORY,
                            mode=member.mode,
                        )
                    )
                case tarfile.SYMTYPE:
                    entries.append(
                        TarEntry(
                            name=member.name,
                            kind=EntryKind.SYMLINK,
                            mode=member.mode,
                            linkname=member.linkname,
                        )
                    )
                case tarfile.LNKTYPE:
                    entries.append(
                        TarEntry(
                            name=member.name,
                            kind=EntryKind.HARDLINK,
                            mode=member.mode,
                            linkname=member.linkname,
                        )
                    )
                case unsupported:
                    assert_never(unsupported)
    return tuple(entries)


def write_entries(archive: Path, entries: tuple[TarEntry, ...]) -> None:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
            for entry in entries:
                info = tarfile.TarInfo(entry.name)
                info.mode = entry.mode
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                match entry.kind:
                    case EntryKind.FILE:
                        info.type = tarfile.REGTYPE
                        info.size = len(entry.payload)
                        bundle.addfile(info, io.BytesIO(entry.payload))
                    case EntryKind.DIRECTORY:
                        info.type = tarfile.DIRTYPE
                        bundle.addfile(info)
                    case EntryKind.SYMLINK:
                        info.type = tarfile.SYMTYPE
                        info.linkname = entry.linkname
                        bundle.addfile(info)
                    case EntryKind.HARDLINK:
                        info.type = tarfile.LNKTYPE
                        info.linkname = entry.linkname
                        bundle.addfile(info)
                    case unreachable:
                        assert_never(unreachable)
    archive.write_bytes(output.getvalue())
