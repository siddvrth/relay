#!/usr/bin/env python3
"""Integration tests for deterministic Relay builds from Git objects."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


BUILDER = Path(__file__).with_name("build_release.py")
PAYLOADS = (
    ".codex-plugin/plugin.json",
    ".codex-plugin/release-files.json",
    "README.md",
    "bin/relay",
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def write_fixture(repo: Path) -> str:
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Relay Tests")
    git(repo, "config", "user.email", "relay-tests@example.invalid")
    (repo / ".codex-plugin").mkdir()
    (repo / "bin").mkdir()
    (repo / ".codex-plugin/plugin.json").write_text(
        '{"name":"relay","version":"1.2.3"}\n',
        encoding="utf-8",
    )
    (repo / ".codex-plugin/release-files.json").write_text(
        json.dumps({"schema_version": 1, "paths": list(PAYLOADS)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("committed payload\n", encoding="utf-8")
    executable = repo / "bin/relay"
    executable.write_text("#!/bin/sh\nprintf 'relay\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD")


def build(repo: Path, commit: str, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def artifacts(output: Path) -> tuple[Path, Path]:
    archives = list(output.glob("*.tar.gz"))
    manifests = list(output.glob("*.manifest.json"))
    if len(archives) != 1 or len(manifests) != 1:
        raise AssertionError(f"expected one artifact pair: {archives!r}, {manifests!r}")
    return archives[0], manifests[0]


class DeterministicBuildTests(unittest.TestCase):
    def test_build_uses_only_selected_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            output.mkdir()
            commit = write_fixture(repo)
            (repo / "README.md").write_text("dirty payload\n", encoding="utf-8")
            (repo / "bin/relay").unlink()
            (repo / ".agents").mkdir()
            (repo / ".agents/private.log").write_text("secret\n", encoding="utf-8")
            status_before = git(repo, "status", "--porcelain=v1", "-z")

            result = build(repo, commit, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            archive, _ = artifacts(output)
            with tarfile.open(archive, "r:gz") as bundle:
                member = bundle.extractfile("relay-1.2.3/README.md")
                self.assertIsNotNone(member)
                assert member is not None
                self.assertEqual(member.read(), b"committed payload\n")
                self.assertNotIn(
                    "relay-1.2.3/.agents/private.log",
                    bundle.getnames(),
                )
            self.assertEqual(
                git(repo, "status", "--porcelain=v1", "-z"),
                status_before,
            )

    def test_repeated_builds_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, first, second = root / "repo", root / "first", root / "second"
            repo.mkdir()
            first.mkdir()
            second.mkdir()
            commit = write_fixture(repo)

            first_result = build(repo, commit, first)
            second_result = build(repo, commit, second)

            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            first_archive, first_manifest = artifacts(first)
            second_archive, second_manifest = artifacts(second)
            self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
            self.assertEqual(first_manifest.read_bytes(), second_manifest.read_bytes())

    def test_manifest_matches_archive_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            output.mkdir()
            commit = write_fixture(repo)

            result = build(repo, commit, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            archive, manifest_path = artifacts(output)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["source_commit"], commit)
            self.assertEqual(manifest["version"], "1.2.3")
            self.assertEqual(manifest["archive"]["path"], archive.name)
            self.assertEqual(manifest["archive"]["size"], archive.stat().st_size)
            self.assertEqual(
                manifest["archive"]["sha256"],
                hashlib.sha256(archive.read_bytes()).hexdigest(),
            )
            expected = []
            for relative in PAYLOADS:
                payload = subprocess.run(
                    ["git", "show", f"{commit}:{relative}"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                ).stdout
                expected.append(
                    {
                        "mode": "0755" if relative == "bin/relay" else "0644",
                        "path": relative,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                )
            self.assertEqual(manifest["files"], expected)
            self.assertNotIn(str(repo).encode(), archive.read_bytes())
            self.assertNotIn(str(repo), manifest_path.read_text(encoding="utf-8"))

    def test_archive_members_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            output.mkdir()
            commit = write_fixture(repo)
            result = build(repo, commit, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            archive, _ = artifacts(output)

            with tarfile.open(fileobj=io.BytesIO(archive.read_bytes()), mode="r:gz") as bundle:
                members = bundle.getmembers()

            self.assertEqual(
                [member.name for member in members],
                [
                    "relay-1.2.3",
                    "relay-1.2.3/.codex-plugin",
                    "relay-1.2.3/bin",
                    *[f"relay-1.2.3/{path}" for path in PAYLOADS],
                ],
            )
            for member in members:
                self.assertEqual(
                    (member.uid, member.gid, member.uname, member.gname, member.mtime),
                    (0, 0, "", "", 0),
                )
                self.assertFalse(member.name.startswith("/"))
            modes = {member.name: member.mode for member in members if member.isfile()}
            self.assertEqual(modes["relay-1.2.3/bin/relay"], 0o755)
            self.assertEqual(modes["relay-1.2.3/README.md"], 0o644)

    def test_invalid_inputs_leave_no_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            output.mkdir()
            commit = write_fixture(repo)
            cases = (
                ("invalid revision", "missing-revision", output),
                ("output inside repo", commit, repo / "dist"),
            )
            for label, revision, destination in cases:
                with self.subTest(label=label):
                    destination.mkdir(exist_ok=True)
                    result = build(repo, revision, destination)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(list(destination.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
