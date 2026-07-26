#!/usr/bin/env python3
"""Integrity and membership tests for the independent release verifier."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from release_test_support import (
    EntryKind,
    TarEntry,
    build_valid_artifacts,
    read_entries,
    write_entries,
)


VERIFIER = Path(__file__).with_name("verify_release.py")


def verify(
    archive: Path,
    manifest: Path,
    expected_commit: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(VERIFIER),
        "--archive",
        str(archive),
        "--manifest",
        str(manifest),
    ]
    if expected_commit is not None:
        command.extend(("--expected-commit", expected_commit))
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )


def sync_archive_record(archive: Path, manifest: Path) -> None:
    document = json.loads(manifest.read_text(encoding="utf-8"))
    payload = archive.read_bytes()
    document["archive"]["sha256"] = hashlib.sha256(payload).hexdigest()
    document["archive"]["size"] = len(payload)
    manifest.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class ReleaseVerifierTests(unittest.TestCase):
    def assert_rejected(self, archive: Path, manifest: Path) -> None:
        result = verify(archive, manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            result.stderr.startswith("release verification failed:"),
            result.stderr,
        )

    def test_accepts_real_builder_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))

            result = verify(built.archive, built.manifest)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "release verified")

    def test_expected_commit_is_a_trust_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))
            document = json.loads(built.manifest.read_text(encoding="utf-8"))
            expected = document["source_commit"]

            accepted = verify(built.archive, built.manifest, expected)
            rejected = verify(built.archive, built.manifest, "0" * 40)

            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertTrue(
                rejected.stderr.startswith("release verification failed:"),
                rejected.stderr,
            )

    def test_rejects_symlinked_inputs(self) -> None:
        for target in ("archive", "manifest"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                built = build_valid_artifacts(root)
                linked = root / f"linked-{target}"
                source = built.archive if target == "archive" else built.manifest
                linked.symlink_to(source)
                archive = linked if target == "archive" else built.archive
                manifest = linked if target == "manifest" else built.manifest

                self.assert_rejected(archive, manifest)

    def test_rejects_modified_archive_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))
            built.archive.write_bytes(built.archive.read_bytes() + b"tampered")

            self.assert_rejected(built.archive, built.manifest)

    def test_rejects_modified_payload_with_rebound_archive_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))
            entries = tuple(
                TarEntry(
                    name=entry.name,
                    kind=entry.kind,
                    mode=entry.mode,
                    payload=b"changed\n" if entry.name.endswith("/README.md") else entry.payload,
                    linkname=entry.linkname,
                )
                for entry in read_entries(built.archive)
            )
            write_entries(built.archive, entries)
            sync_archive_record(built.archive, built.manifest)

            self.assert_rejected(built.archive, built.manifest)

    def test_rejects_missing_archive_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))
            entries = tuple(
                entry
                for entry in read_entries(built.archive)
                if not entry.name.endswith("/README.md")
            )
            write_entries(built.archive, entries)
            sync_archive_record(built.archive, built.manifest)

            self.assert_rejected(built.archive, built.manifest)

    def test_rejects_unexpected_archive_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))
            entries = (
                *read_entries(built.archive),
                TarEntry(
                    name="fresh-handoff-1.2.3/private.txt",
                    kind=EntryKind.FILE,
                    mode=0o644,
                    payload=b"private\n",
                ),
            )
            write_entries(built.archive, entries)
            sync_archive_record(built.archive, built.manifest)

            self.assert_rejected(built.archive, built.manifest)

    def test_rejects_missing_and_unexpected_manifest_files(self) -> None:
        for label in ("missing", "unexpected"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                document = json.loads(built.manifest.read_text(encoding="utf-8"))
                if label == "missing":
                    document["files"].pop()
                else:
                    document["files"].append(
                        {
                            "mode": "0644",
                            "path": "unexpected.txt",
                            "sha256": hashlib.sha256(b"unexpected").hexdigest(),
                            "size": 10,
                        }
                    )
                built.manifest.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_bad_file_hash_and_size(self) -> None:
        for field, value in (("sha256", "0" * 64), ("size", 999)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                document = json.loads(built.manifest.read_text(encoding="utf-8"))
                document["files"][-1][field] = value
                built.manifest.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_malformed_and_duplicate_key_manifests(self) -> None:
        payloads = (
            b"{not json",
            b'{"schema_version":1,"schema_version":1}\\n',
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                built.manifest.write_bytes(payload)

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_noncanonical_manifest_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))
            document = json.loads(built.manifest.read_text(encoding="utf-8"))
            built.manifest.write_text(json.dumps(document, indent=2), encoding="utf-8")

            self.assert_rejected(built.archive, built.manifest)


if __name__ == "__main__":
    unittest.main()
