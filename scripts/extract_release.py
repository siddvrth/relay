#!/usr/bin/env python3
"""Verify and safely materialize a Relay release without tar extraction."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys

from verify_release import VerificationError, VerifiedRelease, verify_release


@dataclass(frozen=True, slots=True)
class ExtractionError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def extract_release(release: VerifiedRelease, destination: Path) -> Path:
    if destination.is_symlink() or not destination.is_dir():
        raise ExtractionError(reason="destination must be a real directory")
    try:
        if any(destination.iterdir()):
            raise ExtractionError(reason="destination must be empty")
    except OSError as error:
        raise ExtractionError(reason=f"cannot inspect destination: {error}") from error
    package = destination / release.root
    try:
        package.mkdir(mode=0o755)
        for item in release.files:
            target = package / item.path
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(item.payload)
            target.chmod(item.mode)
        package.chmod(0o755)
        for directory in (path for path in package.rglob("*") if path.is_dir()):
            directory.chmod(0o755)
    except OSError:
        if package.exists():
            shutil.rmtree(package)
        raise
    return package


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        release = verify_release(
            args.archive,
            args.manifest,
            args.expected_commit,
        )
        package = extract_release(release, args.destination)
    except (ExtractionError, OSError, VerificationError) as error:
        print(f"release extraction failed: {error}", file=sys.stderr)
        return 1
    print(f"release extracted: {package}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
