#!/usr/bin/env python3
"""Safe extraction tests for verified Relay releases."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from release_test_support import build_valid_artifacts


EXTRACTOR = Path(__file__).with_name("extract_release.py")


def extract(
    archive: Path,
    manifest: Path,
    destination: Path,
) -> subprocess.CompletedProcess[str]:
    document = json.loads(manifest.read_text(encoding="utf-8"))
    return subprocess.run(
        [
            sys.executable,
            str(EXTRACTOR),
            "--archive",
            str(archive),
            "--manifest",
            str(manifest),
            "--expected-commit",
            document["source_commit"],
            "--destination",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


class ReleaseExtractionTests(unittest.TestCase):
    def assert_rejected(
        self,
        archive: Path,
        manifest: Path,
        destination: Path,
    ) -> None:
        result = extract(archive, manifest, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            result.stderr.startswith("release extraction failed:"),
            result.stderr,
        )

    def test_extracts_exact_verified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            built = build_valid_artifacts(root)
            destination = root / "extract"
            destination.mkdir()

            result = extract(built.archive, built.manifest, destination)

            self.assertEqual(result.returncode, 0, result.stderr)
            package = destination / "relay-1.2.3"
            self.assertEqual(result.stdout.strip(), f"release extracted: {package}")
            document = json.loads(built.manifest.read_text(encoding="utf-8"))
            files = sorted(
                str(path.relative_to(package))
                for path in package.rglob("*")
                if path.is_file()
            )
            self.assertEqual(files, [item["path"] for item in document["files"]])
            for item in document["files"]:
                path = package / item["path"]
                payload = path.read_bytes()
                self.assertEqual(len(payload), item["size"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), item["sha256"])
                self.assertEqual(path.stat().st_mode & 0o777, int(item["mode"], 8))

    def test_verification_failure_leaves_destination_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            built = build_valid_artifacts(root)
            destination = root / "extract"
            destination.mkdir()
            built.archive.write_bytes(built.archive.read_bytes() + b"tampered")

            self.assert_rejected(built.archive, built.manifest, destination)
            self.assertEqual(list(destination.iterdir()), [])

    def test_rejects_nonempty_and_symlink_destinations(self) -> None:
        for label in ("nonempty", "symlink"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                built = build_valid_artifacts(root)
                destination = root / "extract"
                if label == "nonempty":
                    destination.mkdir()
                    (destination / "sentinel").write_text("keep\n", encoding="utf-8")
                else:
                    target = root / "target"
                    target.mkdir()
                    destination.symlink_to(target, target_is_directory=True)

                self.assert_rejected(built.archive, built.manifest, destination)
                if label == "nonempty":
                    self.assertEqual(
                        (destination / "sentinel").read_text(encoding="utf-8"),
                        "keep\n",
                    )
                else:
                    self.assertTrue(destination.is_symlink())


if __name__ == "__main__":
    unittest.main()
