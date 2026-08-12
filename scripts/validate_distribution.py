"""Validate the files Codex loads from this plugin."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NoReturn


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
POLICY = ROOT / ".codex-plugin" / "release-policy.json"
HOOKS = ROOT / "hooks" / "hooks.json"
STALE_NAMES = (
    "context_handoff.py",
    "write_handoff.py",
    "transfer_control.py",
    "codex_app_delivery_state.py",
    "codex_app_worker.py",
    "scripts/workflow",
    ".agents/skills",
    "capsule_sha256",
    "transfer_nonce",
    "tombstone",
)


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"invalid JSON at {path.relative_to(ROOT)}: {error}")
    if not isinstance(value, dict):
        fail(f"expected JSON object at {path.relative_to(ROOT)}")
    return value


def validate_skill(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        fail(f"invalid skill frontmatter: {path.relative_to(ROOT)}")
    frontmatter = text.split("---\n", 2)[1]
    fields = {
        line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
        for line in frontmatter.splitlines()
        if ":" in line
    }
    if set(fields) != {"name", "description"}:
        fail(f"skill frontmatter must contain only name and description: {path}")
    if fields["name"] != path.parent.name or not fields["description"]:
        fail(f"invalid skill metadata: {path.relative_to(ROOT)}")


def validate() -> None:
    manifest = load_object(MANIFEST)
    if manifest.get("name") != "relay":
        fail("plugin name must be relay")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(manifest.get("version"))):
        fail("plugin version must be semver")
    if manifest.get("skills") != "./skills/":
        fail("plugin skills path must be ./skills/")
    if manifest.get("repository") != "https://github.com/siddvrth/relay":
        fail("plugin repository URL is incorrect")
    if manifest.get("license") != "MIT" or not (ROOT / "LICENSE").is_file():
        fail("MIT license metadata/file is missing")
    if not (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").startswith(
        f"# Changelog\n\n## {manifest['version']}"
    ):
        fail("top changelog heading must match plugin version")

    policy = load_object(POLICY)
    if policy != {
        "schema_version": 1,
        "release_mode": "experimental_non_claim",
        "token_efficiency_claim": False,
        "cost_savings_claim": False,
    }:
        fail("release policy must remain the non-claim contract")

    hooks = load_object(HOOKS).get("hooks")
    if not isinstance(hooks, dict):
        fail("hooks/hooks.json must contain hooks")
    if set(hooks) != {"UserPromptSubmit", "PreToolUse"}:
        fail("plugin must expose only its two functional hooks")
    for event in ("UserPromptSubmit", "PreToolUse"):
        entries = hooks.get(event)
        if not isinstance(entries, list) or not entries:
            fail(f"missing plugin hook: {event}")
        command = json.dumps(entries)
        if "${PLUGIN_ROOT}/hooks/relay_hook.sh" not in command:
            fail(f"{event} hook does not resolve through PLUGIN_ROOT")

    skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
    if [path.parent.name for path in skill_files] != ["relay"]:
        fail("plugin must contain exactly the relay skill")
    for path in skill_files:
        validate_skill(path)

    scan_roots = [
        ROOT / ".codex-plugin",
        ROOT / "hooks",
        ROOT / "skills",
        ROOT / ".github",
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        ROOT / "validate.sh",
    ]
    files: list[Path] = []
    for scan_root in scan_roots:
        if scan_root.is_file():
            files.append(scan_root)
        elif scan_root.is_dir():
            files.extend(path for path in scan_root.rglob("*") if path.is_file())
    for path in files:
        if any(part in {".omx", ".mypy_cache", "__pycache__", ".DS_Store"} for part in path.parts):
            continue
        relative = str(path.relative_to(ROOT))
        if any(stale in relative for stale in STALE_NAMES):
            fail(f"obsolete path remains: {relative}")
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(stale in text for stale in STALE_NAMES):
            fail(f"obsolete reference remains: {relative}")
        if "[TODO:" in text:
            fail(f"TODO placeholder remains: {relative}")


def main() -> int:
    try:
        validate()
    except ValueError as error:
        print(f"distribution validation failed: {error}", file=sys.stderr)
        return 1
    print("distribution validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
