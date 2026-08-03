#!/usr/bin/env python3
"""Check the plugin and skill layout has no undeclared dependencies."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NoReturn

from release_contract import validate_release_worktree


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
HOOKS = ROOT / "hooks" / "hooks.json"
RELEASE_POLICY = ROOT / ".codex-plugin" / "release-policy.json"
SKILLS = ROOT / "skills"


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"invalid JSON at {path.relative_to(ROOT)}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected object at {path.relative_to(ROOT)}")
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
        fail(
            "skill frontmatter must contain only name and description: "
            f"{path.relative_to(ROOT)}"
        )
    if fields["name"] != path.parent.name:
        fail(f"skill name must match directory: {path.relative_to(ROOT)}")
    if not fields["description"]:
        fail(f"skill description is empty: {path.relative_to(ROOT)}")


def validate() -> None:
    validate_release_worktree(ROOT)
    manifest = load_json(MANIFEST)
    for key in ("name", "version", "description", "author", "skills", "interface"):
        if not manifest.get(key):
            fail(f"plugin manifest missing {key}")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(manifest["name"])):
        fail("plugin name must be kebab-case")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(manifest["version"])):
        fail("plugin version must be semver")
    if manifest["skills"] != "./skills/":
        fail("plugin skills path must be ./skills/")
    if manifest["name"] != "relay":
        fail("plugin name must be relay")
    if manifest.get("repository") != "https://github.com/siddvrth/relay":
        fail("plugin repository URL must match the planned relay remote")
    if manifest.get("license") != "MIT":
        fail("plugin license must be MIT")
    if not (ROOT / "LICENSE").is_file():
        fail("MIT license file is missing")
    release_policy = load_json(RELEASE_POLICY)
    if release_policy != {
        "schema_version": 1,
        "release_mode": "experimental_non_claim",
        "token_efficiency_claim": False,
        "cost_savings_claim": False,
    }:
        fail("release policy must be the exact experimental non-claim contract")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## {manifest['version']}" not in changelog:
        fail("plugin version must have a matching changelog release heading")

    skill_files = sorted(SKILLS.glob("*/SKILL.md"))
    if not skill_files:
        fail("plugin contains no skills")
    for skill_file in skill_files:
        validate_skill(skill_file)

    hooks = load_json(HOOKS).get("hooks")
    if not isinstance(hooks, dict):
        fail("hooks/hooks.json must contain a hooks object")
    for event in ("UserPromptSubmit", "PreToolUse", "PreCompact", "Stop"):
        if event not in hooks:
            fail(f"plugin hooks missing {event}")
    if "${PLUGIN_ROOT}" not in HOOKS.read_text(encoding="utf-8"):
        fail("plugin hooks must resolve commands through PLUGIN_ROOT")

    distribution_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in [MANIFEST, HOOKS, *skill_files]
    )
    if "[TODO:" in distribution_text:
        fail("distribution contains TODO placeholders")


def main() -> int:
    try:
        validate()
    except ValueError as exc:
        print(f"distribution validation failed: {exc}", file=sys.stderr)
        return 1
    print("distribution validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
