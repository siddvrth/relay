"""Require a fresh Relay release identity when packaged runtime changes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ".codex-plugin/plugin.json"
PROTOCOL = ROOT / "skills" / "relay" / "scripts" / "codex_app_protocol.py"
RUNTIME_PREFIXES = (".codex-plugin/", "hooks/", "skills/relay/")


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def version_at(ref: str) -> str:
    value = json.loads(run("git", "show", f"{ref}:{MANIFEST}"))
    version = value.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"invalid plugin version at {ref}")
    return version


def current_version() -> str:
    value = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
    version = value.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("invalid current plugin version")
    return version


def version_core(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", version)
    if match is None:
        raise ValueError(f"invalid semver: {version}")
    return tuple(int(part) for part in match.groups())


def packaged_runtime(path: str) -> bool:
    if not path.startswith(RUNTIME_PREFIXES):
        return False
    name = Path(path).name
    return not name.startswith("test_") and name != "smoke_codex_app_transport.py"


def require_client_version(version: str) -> None:
    text = PROTOCOL.read_text(encoding="utf-8")
    match = re.search(r'CLIENT_INFO:.*?"version"\s*:\s*"([^"]+)"', text, re.S)
    if match is None or match.group(1) != version:
        raise ValueError("app-server client version must match the plugin version")


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("usage: check_release_bump.py <base-ref>", file=sys.stderr)
        return 2
    try:
        base = sys.argv[1]
        head_version = current_version()
        require_client_version(head_version)
        changed = run("git", "diff", "--name-only", f"{base}...HEAD").splitlines()
        runtime_changed = sorted(path for path in changed if packaged_runtime(path))
        if not runtime_changed:
            print("release bump guard: no packaged runtime changes")
            return 0
        base_version = version_at(base)
        if version_core(head_version) <= version_core(base_version):
            print(
                "release bump guard failed: packaged runtime changed without a newer plugin version",
                file=sys.stderr,
            )
            for path in runtime_changed:
                print(f"  {path}", file=sys.stderr)
            print(f"version is {base_version} -> {head_version}", file=sys.stderr)
            return 1
        print(f"release bump guard: {base_version} -> {head_version}")
        return 0
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, ValueError) as error:
        print(f"release bump guard failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
