#!/usr/bin/env python3
"""Path, archive-type, and metadata attacks against release verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from release_test_support import (
    EntryKind,
    TarEntry,
    build_valid_artifacts,
    read_entries,
    write_entries,
)
from test_verify_release import sync_archive_record, verify


class AdversarialReleaseVerifierTests(unittest.TestCase):
    def assert_rejected(self, archive: Path, manifest: Path) -> None:
        result = verify(archive, manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            result.stderr.startswith("release verification failed:"),
            result.stderr,
        )

    def test_rejects_unsafe_archive_paths(self) -> None:
        unsafe = (
            "/absolute.txt",
            "relay-1.2.3/../escape.txt",
            "relay-1.2.3/./dot.txt",
            "relay-1.2.3\\backslash.txt",
        )
        for name in unsafe:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                entries = (
                    *read_entries(built.archive),
                    TarEntry(
                        name=name,
                        kind=EntryKind.FILE,
                        mode=0o644,
                        payload=b"attack\n",
                    ),
                )
                write_entries(built.archive, entries)
                sync_archive_record(built.archive, built.manifest)

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_symlinks_and_hardlinks(self) -> None:
        links = (
            (EntryKind.SYMLINK, "../../outside"),
            (EntryKind.HARDLINK, "relay-1.2.3/README.md"),
        )
        for kind, target in links:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                entries = (
                    *read_entries(built.archive),
                    TarEntry(
                        name=f"relay-1.2.3/{kind.value}",
                        kind=kind,
                        mode=0o777,
                        linkname=target,
                    ),
                )
                write_entries(built.archive, entries)
                sync_archive_record(built.archive, built.manifest)

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_duplicate_and_case_ambiguous_members(self) -> None:
        for label, duplicate in (
            ("duplicate", "relay-1.2.3/README.md"),
            ("casefold", "relay-1.2.3/readme.md"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                entries = (
                    *read_entries(built.archive),
                    TarEntry(
                        name=duplicate,
                        kind=EntryKind.FILE,
                        mode=0o644,
                        payload=b"ambiguous\n",
                    ),
                )
                write_entries(built.archive, entries)
                sync_archive_record(built.archive, built.manifest)

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_trailing_and_concatenated_gzip_data(self) -> None:
        for label in ("trailing", "concatenated"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                original = built.archive.read_bytes()
                suffix = b"trailing-data" if label == "trailing" else original
                built.archive.write_bytes(original + suffix)
                sync_archive_record(built.archive, built.manifest)

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_invalid_source_and_version_metadata(self) -> None:
        mutations = (
            ("source_commit", "abc"),
            ("source_commit", "A" * 40),
            ("version", "1.2"),
            ("version", "../1.2.3"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                document = json.loads(built.manifest.read_text(encoding="utf-8"))
                document[field] = value
                built.manifest.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_manifest_shape_and_field_types(self) -> None:
        mutations = (
            ("extra", True),
            ("schema_version", True),
            ("files", {}),
            ("archive", []),
        )
        for field, value in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                document = json.loads(built.manifest.read_text(encoding="utf-8"))
                document[field] = value
                built.manifest.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_unsafe_manifest_paths_and_modes(self) -> None:
        mutations = (
            ("path", "../escape.txt"),
            ("path", "/absolute.txt"),
            ("path", "private\\file.txt"),
            ("mode", "0777"),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                document = json.loads(built.manifest.read_text(encoding="utf-8"))
                document["files"][-1][field] = value
                built.manifest.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                self.assert_rejected(built.archive, built.manifest)

if __name__ == "__main__":
    unittest.main()
