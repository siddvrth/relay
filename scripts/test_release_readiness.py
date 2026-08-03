#!/usr/bin/env python3
"""Focused regression tests for the release-readiness checks."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("check_release_readiness.py")
PROJECT_ROOT = MODULE_PATH.parents[1]
COPYTREE_IGNORE = shutil.ignore_patterns(
    ".git",
    ".agents",
    ".codegraph",
    ".mypy_cache",
    ".omo",
    ".omx",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
    "__pycache__",
)
SPEC = importlib.util.spec_from_file_location("check_release_readiness", MODULE_PATH)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def make_release(temp: str) -> Path:
    root = Path(temp) / "release"
    shutil.copytree(PROJECT_ROOT, root, ignore=COPYTREE_IGNORE)
    run_git(root, "init", "-q")
    run_git(root, "config", "user.name", "Relay test")
    run_git(root, "config", "user.email", "relay-test@example.com")
    run_git(root, "add", ".")
    run_git(root, "commit", "-qm", "release fixture")
    run_git(root, "remote", "add", "origin", "https://github.com/siddvrth/relay")
    return root


def write_live_hook_evidence(root: Path) -> Path:
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = json.loads(
        (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    adapters = ("hooks/relay_hook.sh", "codex/relay_hook.sh")
    value = {
        "schema_version": 1,
        "evidence_type": "codex_live_hooks_trust",
        "checked_via": "/hooks",
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "loaded": True,
        "trusted": True,
        "hook_events": ["UserPromptSubmit", "PreToolUse", "PreCompact", "Stop"],
        "plugin_version": manifest["version"],
        "hooks_json_sha256": digest(root / "hooks" / "hooks.json"),
        "adapter_sha256s": {adapter: digest(root / adapter) for adapter in adapters},
    }
    path = root / "artifacts" / "metrics" / "live-hooks-trust.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


class ReleaseReadinessTests(unittest.TestCase):
    def test_clean_committed_release_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = release.assess(make_release(temp))

        self.assertTrue(result["ready"], result["blockers"])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["release_mode"], "experimental_non_claim")
        self.assertEqual(result["positive_claims"], [])

    def test_missing_manifest_is_a_structured_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = make_release(temp)
            (root / ".codex-plugin" / "plugin.json").unlink()
            run_git(root, "add", ".codex-plugin/plugin.json")
            run_git(root, "commit", "-qm", "remove manifest")
            result = release.assess(root)

        self.assertFalse(result["ready"])
        self.assertIn("plugin manifest is missing", result["blockers"])

    def test_positive_public_claim_blocks_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = make_release(temp)
            (root / "README.md").write_text(
                "# Relay\n\nRelay saves tokens and lowers costs.\n",
                encoding="utf-8",
            )
            run_git(root, "add", "README.md")
            run_git(root, "commit", "-qm", "claiming release")
            result = release.assess(root)

        self.assertFalse(result["ready"])
        self.assertTrue(result["positive_claims"])
        self.assertTrue(
            any("positive token/cost claim" in item for item in result["blockers"])
        )

    def test_invalid_release_policy_blocks_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = make_release(temp)
            policy = root / ".codex-plugin" / "release-policy.json"
            policy.write_text('{"schema_version": 1}\n', encoding="utf-8")
            run_git(root, "add", str(policy.relative_to(root)))
            run_git(root, "commit", "-qm", "invalid policy")
            result = release.assess(root)

        self.assertFalse(result["ready"])
        self.assertTrue(any("release policy invalid" in item for item in result["blockers"]))

    def test_claim_classifier_handles_negative_and_positive_statements(self) -> None:
        self.assertFalse(release._contains_positive_claim("Relay does not reduce goal tokens."))
        self.assertFalse(release._contains_positive_claim("This is not evidence that Relay saves tokens."))
        self.assertTrue(release._contains_positive_claim("Relay saves tokens."))

    def test_live_hook_evidence_is_validated_against_current_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = make_release(temp)
            evidence = write_live_hook_evidence(root)
            result = release.assess_live_hooks_trust(root)
            evidence.write_text(
                evidence.read_text(encoding="utf-8").replace(
                    '"trusted": true', '"trusted": false'
                ),
                encoding="utf-8",
            )
            invalid = release.assess_live_hooks_trust(root)

        self.assertTrue(result["ready"], result["error"])
        self.assertFalse(invalid["ready"])


if __name__ == "__main__":
    unittest.main()
