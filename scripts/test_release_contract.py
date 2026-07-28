#!/usr/bin/env python3
"""Tests for the public release file allowlist."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BUILDER = Path(__file__).with_name("build_release.py")
BASE_PATHS = (
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


def initialize_repo(repo: Path, paths: tuple[str, ...] = BASE_PATHS) -> str:
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Relay Tests")
    git(repo, "config", "user.email", "relay-tests@example.invalid")
    plugin = repo / ".codex-plugin"
    plugin.mkdir()
    (repo / "bin").mkdir()
    (plugin / "plugin.json").write_text(
        '{"name":"relay","version":"1.2.3"}\n',
        encoding="utf-8",
    )
    (plugin / "release-files.json").write_text(
        json.dumps({"schema_version": 1, "paths": list(paths)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("public release\n", encoding="utf-8")
    executable = repo / "bin/relay"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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


def listed(path: str) -> tuple[str, ...]:
    return tuple(sorted((*BASE_PATHS, path)))


class ReleaseContractTests(unittest.TestCase):
    def test_sorted_exact_allowlist_builds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            output.mkdir()
            commit = initialize_repo(repo)

            result = build(repo, commit, output)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list(output.glob("*.tar.gz"))), 1)
            self.assertEqual(len(list(output.glob("*.manifest.json"))), 1)

    def test_invalid_contract_paths_fail_closed(self) -> None:
        invalid = {
            "duplicate": listed("README.md"),
            "unsorted": tuple(reversed(BASE_PATHS)),
            "absolute": listed("/private/evidence.json"),
            "parent": listed("docs/../private.json"),
            "dot": listed("./private.json"),
            "backslash": listed("private\\evidence.json"),
            "nul": listed("private\u0000evidence.json"),
            "agents": listed(".agents/private.json"),
            "omo": listed(".omo/private.json"),
            "omx": listed(".omx/private.json"),
            "private": listed("private/evidence.json"),
            "cache_dir": listed("src/__pycache__/module.py"),
            "pyc": listed("src/module.pyc"),
            "pyo": listed("src/module.pyo"),
            "raw_log": listed("artifacts/raw-agent.log"),
            "tmp": listed("artifacts/result.tmp"),
            "temp": listed("artifacts/result.temp"),
            "backup": listed("README.md~"),
            "machine": listed(".DS_Store"),
            "environment": listed(".env"),
        }
        for label, paths in invalid.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo, output = root / "repo", root / "output"
                repo.mkdir()
                output.mkdir()
                commit = initialize_repo(repo, paths)

                result = build(repo, commit, output)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("release contract", result.stderr.lower())
                self.assertEqual(list(output.iterdir()), [])

    def test_missing_contract_at_commit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, output = root / "repo", root / "output"
            repo.mkdir()
            output.mkdir()
            initialize_repo(repo)
            git(repo, "rm", ".codex-plugin/release-files.json")
            git(repo, "commit", "-qm", "remove contract")
            commit = git(repo, "rev-parse", "HEAD")

            result = build(repo, commit, output)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("release-files.json", result.stderr)
            self.assertEqual(list(output.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
