#!/usr/bin/env python3
"""Artifact-only fresh-consumer acceptance test for Relay releases."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_release.py"
EXTRACTOR = ROOT / "scripts" / "extract_release.py"
SESSION_ID = "release-consumer"
NEXT_ACTION = "Run python3 -V after resume"


def run(
    command: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


class FreshReleaseConsumerTests(unittest.TestCase):
    def assert_passed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_fresh_consumer_installs_and_checkpoints_from_release(self) -> None:
        source_status = run(["git", "status", "--porcelain=v1", "-z"], ROOT)
        self.assert_passed(source_status)
        commit_result = run(["git", "rev-parse", "HEAD"], ROOT)
        self.assert_passed(commit_result)
        commit = commit_result.stdout.strip()
        with tempfile.TemporaryDirectory(prefix="relay-consumer-") as temp:
            workspace = Path(temp)
            output = workspace / "output"
            extraction = workspace / "extraction"
            consumer = workspace / "consumer"
            output.mkdir()
            extraction.mkdir()
            consumer.mkdir()

            built = run(
                [
                    sys.executable,
                    str(BUILDER),
                    "--repo",
                    str(ROOT),
                    "--commit",
                    commit,
                    "--output-dir",
                    str(output),
                ]
            )
            self.assert_passed(built)
            archive = next(output.glob("*.tar.gz"))
            manifest_path = next(output.glob("*.manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            extracted = run(
                [
                    sys.executable,
                    str(EXTRACTOR),
                    "--archive",
                    str(archive),
                    "--manifest",
                    str(manifest_path),
                    "--expected-commit",
                    commit,
                    "--destination",
                    str(extraction),
                ]
            )
            self.assert_passed(extracted)
            package = extraction / f"relay-{manifest['version']}"
            self.assertTrue(package.is_dir())

            verified = run(
                [
                    sys.executable,
                    str(package / "scripts" / "verify_release.py"),
                    "--archive",
                    str(archive),
                    "--manifest",
                    str(manifest_path),
                    "--expected-commit",
                    commit,
                ]
            )
            self.assert_passed(verified)
            self.assert_passed(run(["git", "init", "-q"], consumer))
            self.assert_passed(run(["bash", str(package / "install.sh"), str(consumer)]))
            self.assert_passed(run(["bash", str(package / "audit_install.sh"), str(consumer)]))
            gate_environment = dict(os.environ)
            gate_environment["RELAY_SKIP_RELEASE_CONSUMER"] = "1"
            self.assert_passed(
                run(
                    ["bash", str(package / "completion_gate.sh"), str(consumer)],
                    env=gate_environment,
                )
            )

            installed = consumer / ".agents" / "skills" / "relay" / "scripts"
            seeded = run(
                [
                    sys.executable,
                    str(installed / "write_handoff.py"),
                    "--repo",
                    str(consumer),
                    "--session-id",
                    SESSION_ID,
                    "--revision",
                    "1",
                    "--update-active-task-only",
                    "--objective",
                    "Verify release consumer lifecycle",
                    "--active-task",
                    "Create a resume-ready checkpoint",
                    "--phase",
                    "validation",
                    "--status",
                    "checkpoint pending",
                    "--completion-criteria",
                    "Manual checkpoint is resume-ready",
                    "--remaining-work",
                    "Run recorded resume validation",
                    "--constraints",
                    "Use only the installed release runtime",
                    "--authoritative-files",
                    "scripts/workflow/relay_hook.sh",
                    "--resume-validation-command",
                    "python3 -V",
                    "--resume-validation-expected",
                    "exit 0",
                    "--next-action",
                    NEXT_ACTION,
                ]
            )
            self.assert_passed(seeded)
            checkpoint = run(
                [
                    sys.executable,
                    str(installed / "context_handoff.py"),
                    "--repo",
                    str(consumer),
                    "--session-id",
                    SESSION_ID,
                    "--trigger",
                    "manual",
                    "--force-handoff",
                    "--reason",
                    "fresh consumer checkpoint",
                ]
            )
            self.assert_passed(checkpoint)
            result = json.loads(checkpoint.stdout)
            self.assertTrue(result["resume_ready"])
            self.assertTrue(result["checkpoint_written"])
            self.assertTrue(result["revision_created"])
            self.assertTrue(result["delivery_emitted"])
            self.assertEqual(result["revision"], 1)
            self.assertEqual(result["next_action"], NEXT_ACTION)
            self.assertEqual(
                result["resume_validation"],
                {"command": "python3 -V", "expected": "exit 0"},
            )
            capsule = Path(result["capsule_path"])
            self.assertTrue(capsule.is_file())
            self.assertEqual(
                hashlib.sha256(capsule.read_bytes()).hexdigest(),
                result["capsule_sha256"],
            )
            pointer_path = (
                consumer
                / ".omx"
                / "state"
                / "relay"
                / "sessions"
                / result["session_scope"]
                / ".pointer.json"
            )
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            for field in (
                "capsule_path",
                "capsule_sha256",
                "goal_identity",
                "revision",
                "session_id",
                "transfer_id",
                "transfer_nonce",
            ):
                self.assertEqual(pointer[field], result[field])
            self.assert_passed(run(["python3", "-V"], consumer))
            self.assert_passed(run(["bash", str(package / "audit_install.sh"), str(consumer)]))

        final_status = run(["git", "status", "--porcelain=v1", "-z"], ROOT)
        self.assert_passed(final_status)
        self.assertEqual(final_status.stdout, source_status.stdout)


if __name__ == "__main__":
    unittest.main()
