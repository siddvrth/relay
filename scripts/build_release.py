#!/usr/bin/env python3
"""Build a deterministic Relay source archive from one Git commit."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
from typing import Final

from release_contract import CONTRACT_PATH, ReleaseContractError, parse_release_contract


PLUGIN_PATH: Final = ".codex-plugin/plugin.json"
VERSION_PATTERN: Final = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")


@dataclass(frozen=True, slots=True)
class ReleaseBuildError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class GitBlob:
    mode: str
    oid: str
    payload: bytes


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBuildError(reason=detail or f"git {' '.join(args)} failed")
    return result.stdout


def _resolve_commit(repo: Path, revision: str) -> str:
    value = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    commit = value.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseBuildError(reason="Git did not return a full commit SHA")
    return commit


def _tree(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    output = _git(repo, "ls-tree", "-rz", "--full-tree", commit)
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        if path in entries:
            raise ReleaseBuildError(reason=f"duplicate Git tree path: {path}")
        if kind == "blob":
            entries[path] = (mode, oid)
    return entries


def _blob(repo: Path, tree: dict[str, tuple[str, str]], path: str) -> GitBlob:
    entry = tree.get(path)
    if entry is None:
        raise ReleaseBuildError(reason=f"required release file is missing: {path}")
    mode, oid = entry
    if mode not in {"100644", "100755"}:
        raise ReleaseBuildError(reason=f"unsupported release file mode {mode}: {path}")
    return GitBlob(mode=mode, oid=oid, payload=_git(repo, "cat-file", "blob", oid))


def _version(plugin: bytes) -> str:
    try:
        document = json.loads(plugin)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseBuildError(reason=f"invalid {PLUGIN_PATH}: {error}") from error
    if not isinstance(document, dict) or document.get("name") != "relay":
        raise ReleaseBuildError(reason=f"{PLUGIN_PATH} has invalid name")
    version = document.get("version")
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise ReleaseBuildError(reason=f"{PLUGIN_PATH} has invalid version")
    return version


def _directories(paths: tuple[str, ...]) -> list[str]:
    directories: set[str] = set()
    for raw in paths:
        parent = PurePosixPath(raw).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return sorted(directories)


def _tar_info(name: str, mode: int, *, directory: bool, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.size = size
    return info


def _archive(root: str, paths: tuple[str, ...], blobs: dict[str, GitBlob]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
            bundle.addfile(_tar_info(root, 0o755, directory=True))
            for directory in _directories(paths):
                bundle.addfile(_tar_info(f"{root}/{directory}", 0o755, directory=True))
            for path in paths:
                blob = blobs[path]
                mode = 0o755 if blob.mode == "100755" else 0o644
                bundle.addfile(
                    _tar_info(f"{root}/{path}", mode, directory=False, size=len(blob.payload)),
                    io.BytesIO(blob.payload),
                )
    return output.getvalue()


def _publish(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def build_release(repo: Path, revision: str, output_dir: Path) -> tuple[Path, Path]:
    repository = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    output = output_dir.resolve()
    if not output.is_dir() or output == repository or repository in output.parents:
        raise ReleaseBuildError(reason="output directory must exist outside the repository")
    commit = _resolve_commit(repository, revision)
    tree = _tree(repository, commit)
    contract_blob = _blob(repository, tree, CONTRACT_PATH)
    try:
        contract = parse_release_contract(contract_blob.payload)
    except ReleaseContractError as error:
        raise ReleaseBuildError(reason=str(error)) from error
    blobs = {path: _blob(repository, tree, path) for path in contract.paths}
    version = _version(blobs[PLUGIN_PATH].payload)
    stem = f"relay-{version}-{commit}"
    archive_path = output / f"{stem}.tar.gz"
    manifest_path = output / f"{stem}.manifest.json"
    if archive_path.exists() or manifest_path.exists():
        raise ReleaseBuildError(reason="release output already exists")
    archive = _archive(f"relay-{version}", contract.paths, blobs)
    files = [
        {
            "mode": "0755" if blobs[path].mode == "100755" else "0644",
            "path": path,
            "sha256": hashlib.sha256(blobs[path].payload).hexdigest(),
            "size": len(blobs[path].payload),
        }
        for path in contract.paths
    ]
    manifest = {
        "archive": {
            "path": archive_path.name,
            "sha256": hashlib.sha256(archive).hexdigest(),
            "size": len(archive),
        },
        "files": files,
        "schema_version": 1,
        "source_commit": commit,
        "version": version,
    }
    encoded_manifest = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    try:
        _publish(archive_path, archive)
        _publish(manifest_path, encoded_manifest)
    except FileExistsError as error:
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise ReleaseBuildError(reason="release output already exists") from error
    except OSError:
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    return archive_path, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        archive, manifest = build_release(args.repo, args.commit, args.output_dir)
    except (ReleaseBuildError, OSError) as error:
        print(f"release build failed: {error}", file=sys.stderr)
        return 1
    print(archive.name)
    print(manifest.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
