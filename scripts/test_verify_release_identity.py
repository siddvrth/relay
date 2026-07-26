#!/usr/bin/env python3
"""Embedded contract and plugin attacks against release verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from release_test_support import (
    TarEntry,
    build_valid_artifacts,
    read_entries,
    write_entries,
)
from test_verify_release import verify


class ReleaseIdentityVerifierTests(unittest.TestCase):
    def assert_rejected(self, archive: Path, manifest: Path) -> None:
        result = verify(archive, manifest)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            result.stderr.startswith("release verification failed:"),
            result.stderr,
        )

    def test_rejects_plugin_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            built = build_valid_artifacts(Path(temp))
            entries = []
            plugin_payload = b'{"name":"fresh-handoff","version":"9.9.9"}\n'
            for entry in read_entries(built.archive):
                payload = (
                    plugin_payload
                    if entry.name.endswith("/.codex-plugin/plugin.json")
                    else entry.payload
                )
                entries.append(
                    TarEntry(
                        name=entry.name,
                        kind=entry.kind,
                        mode=entry.mode,
                        payload=payload,
                        linkname=entry.linkname,
                    )
                )
            write_entries(built.archive, tuple(entries))
            document = json.loads(built.manifest.read_text(encoding="utf-8"))
            plugin = next(
                item
                for item in document["files"]
                if item["path"] == ".codex-plugin/plugin.json"
            )
            plugin["sha256"] = hashlib.sha256(plugin_payload).hexdigest()
            plugin["size"] = len(plugin_payload)
            archive_payload = built.archive.read_bytes()
            document["archive"]["sha256"] = hashlib.sha256(archive_payload).hexdigest()
            document["archive"]["size"] = len(archive_payload)
            built.manifest.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

            self.assert_rejected(built.archive, built.manifest)

    def test_rejects_missing_embedded_identity_files(self) -> None:
        for relative in (
            ".codex-plugin/plugin.json",
            ".codex-plugin/release-files.json",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                entries = tuple(
                    entry
                    for entry in read_entries(built.archive)
                    if not entry.name.endswith(f"/{relative}")
                )
                write_entries(built.archive, entries)
                document = json.loads(built.manifest.read_text(encoding="utf-8"))
                document["files"] = [
                    item for item in document["files"] if item["path"] != relative
                ]
                payload = built.archive.read_bytes()
                document["archive"]["sha256"] = hashlib.sha256(payload).hexdigest()
                document["archive"]["size"] = len(payload)
                built.manifest.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                self.assert_rejected(built.archive, built.manifest)

    def test_rejects_duplicate_embedded_json_keys(self) -> None:
        cases = (
            (
                ".codex-plugin/plugin.json",
                b'{"name":"fresh-handoff","name":"fresh-handoff","version":"1.2.3"}\n',
            ),
            (
                ".codex-plugin/release-files.json",
                b'{"schema_version":1,"schema_version":1,"paths":[]}\n',
            ),
        )
        for relative, replacement in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                built = build_valid_artifacts(Path(temp))
                entries = tuple(
                    TarEntry(
                        name=entry.name,
                        kind=entry.kind,
                        mode=entry.mode,
                        payload=(
                            replacement
                            if entry.name.endswith(f"/{relative}")
                            else entry.payload
                        ),
                        linkname=entry.linkname,
                    )
                    for entry in read_entries(built.archive)
                )
                write_entries(built.archive, entries)
                document = json.loads(built.manifest.read_text(encoding="utf-8"))
                item = next(
                    value for value in document["files"] if value["path"] == relative
                )
                item["sha256"] = hashlib.sha256(replacement).hexdigest()
                item["size"] = len(replacement)
                payload = built.archive.read_bytes()
                document["archive"]["sha256"] = hashlib.sha256(payload).hexdigest()
                document["archive"]["size"] = len(payload)
                built.manifest.write_text(
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

                self.assert_rejected(built.archive, built.manifest)


if __name__ == "__main__":
    unittest.main()
