#!/usr/bin/env python3
"""Validate the dependency-free plugin and skill distribution contract."""

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
sys.path.insert(0, str(SKILLS / "relay" / "scripts"))
from goal_telemetry_report import (  # noqa: E402
    V2_SCHEMA_VERSION,
    V2_STUDY_TYPE,
    V3_SCHEMA_VERSION,
    V3_STUDY_TYPE,
    build_v2_report,
    build_v3_report,
    validate_study_document,
    validate_v3_study_document,
)


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
        fail(f"skill frontmatter must contain only name and description: {path.relative_to(ROOT)}")
    if fields["name"] != path.parent.name:
        fail(f"skill name must match directory: {path.relative_to(ROOT)}")
    if not fields["description"]:
        fail(f"skill description is empty: {path.relative_to(ROOT)}")


def valid_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def validate_legacy_goal_telemetry(
    document: dict[str, object],
    path: Path,
    *,
    root: Path = ROOT,
) -> None:
    display = relative_display(path, root)
    if document.get("schema_version") != 1:
        fail(f"historical goal telemetry schema_version must be 1: {display}")
    trials = document.get("trials")
    if not isinstance(trials, list) or not trials:
        fail(f"historical goal telemetry must contain trial rows: {display}")
    seen_pairs: set[int] = set()
    seen_ids: set[str] = set()
    for index, trial in enumerate(trials):
        if not isinstance(trial, dict):
            fail(f"historical goal telemetry row {index} must be an object")
        required = {
            "pair",
            "trial_id",
            "task_shape",
            "clean_tokens",
            "fork_tokens",
            "quality_match",
        }
        missing = sorted(required - set(trial))
        if missing:
            fail(
                f"historical goal telemetry row {index} missing {', '.join(missing)}"
            )
        pair = trial["pair"]
        trial_id = trial["trial_id"]
        if not isinstance(pair, int) or isinstance(pair, bool) or pair < 1:
            fail(f"historical goal telemetry row {index} pair must be a positive integer")
        if pair in seen_pairs:
            fail(f"historical goal telemetry pair {pair} is duplicated")
        seen_pairs.add(pair)
        if not isinstance(trial_id, str) or not trial_id.strip():
            fail(f"historical goal telemetry row {index} trial_id must be non-empty")
        if trial_id in seen_ids:
            fail(f"historical goal telemetry trial_id {trial_id} is duplicated")
        seen_ids.add(trial_id)
        if not isinstance(trial["task_shape"], str) or not trial["task_shape"].strip():
            fail(f"historical goal telemetry row {index} task_shape must be non-empty")
        for key in ("clean_tokens", "fork_tokens"):
            if not valid_nonnegative_int(trial[key]):
                fail(f"historical goal telemetry row {index} {key} must be non-negative")
        if not isinstance(trial["quality_match"], bool):
            fail(f"historical goal telemetry row {index} quality_match must be boolean")
        if "quality_note" in trial and not isinstance(trial["quality_note"], str):
            fail(f"historical goal telemetry row {index} quality_note must be text")


def validate_goal_telemetry_artifacts(root: Path = ROOT) -> dict[str, int]:
    metrics_root = root / "artifacts" / "metrics"
    legacy_path = metrics_root / "20260711-goal-telemetry-20-pairs.json"
    if not legacy_path.is_file():
        fail("historical goal telemetry artifact is missing")
    try:
        legacy_value = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"invalid JSON at {legacy_path.relative_to(root)}: {exc}")
    if not isinstance(legacy_value, dict):
        fail(f"expected object at {legacy_path.relative_to(root)}")
    validate_legacy_goal_telemetry(legacy_value, legacy_path, root=root)

    v2_count = 0
    v3_count = 0
    for path in sorted(metrics_root.glob("*.json")):
        if path == legacy_path or path.name == "live-hooks-trust.json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"invalid JSON at {path.relative_to(root)}: {exc}")
        if not isinstance(value, dict):
            fail(f"expected object at {path.relative_to(root)}")
        identity = (value.get("schema_version"), value.get("study_type"))
        v2_marker = (
            value.get("schema_version") == V2_SCHEMA_VERSION
            or value.get("study_type") == V2_STUDY_TYPE
        )
        v3_marker = (
            value.get("schema_version") == V3_SCHEMA_VERSION
            or value.get("study_type") == V3_STUDY_TYPE
        )
        if not v2_marker and not v3_marker:
            continue
        if identity == (V2_SCHEMA_VERSION, V2_STUDY_TYPE):
            try:
                validate_study_document(value)
                build_v2_report(value)
            except ValueError as exc:
                fail(f"invalid v2 goal telemetry at {path.relative_to(root)}: {exc}")
            v2_count += 1
        elif identity == (V3_SCHEMA_VERSION, V3_STUDY_TYPE):
            try:
                validate_v3_study_document(value)
                build_v3_report(value)
            except ValueError as exc:
                fail(f"invalid v3 goal telemetry at {path.relative_to(root)}: {exc}")
            v3_count += 1
        elif v3_marker:
            fail(
                f"invalid v3 goal telemetry at {path.relative_to(root)}: "
                "mixed or partial schema/type markers"
            )
        else:
            fail(
                f"invalid v2 goal telemetry at {path.relative_to(root)}: "
                "mixed or partial schema/type markers"
            )
    return {
        "historical_trial_count": len(legacy_value["trials"]),
        "v2_study_count": v2_count,
        "v3_study_count": v3_count,
    }


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
    if manifest.get("repository") != "https://github.com/siddvrth/fresh-handoff":
        fail("plugin repository URL must match the planned fresh-handoff remote")
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

    validate_goal_telemetry_artifacts(ROOT)


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
