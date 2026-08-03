#!/usr/bin/env python3
"""Report public-release blockers without changing the repo."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
RELEASE_POLICY_KEYS = {
    "schema_version",
    "release_mode",
    "token_efficiency_claim",
    "cost_savings_claim",
}
LIVE_HOOK_ADAPTERS = (
    "hooks/relay_hook.sh",
    "codex/relay_hook.sh",
)
PUBLIC_TEXT_PATTERNS = (
    "**/*.md",
    "**/*.yaml",
    "**/*.yml",
)
POSITIVE_CLAIM_PATTERN = re.compile(
    r"(?:"
    r"\b(?:saves?|saved|reduces?|reduced|lowers?|lowered|cuts?)\b"
    r"[^.!?]{0,100}?\b(?:tokens?|goal(?:-|\s+)tokens?(?:\s+use)?|costs?)\b"
    r"|"
    r"\b(?:uses?|used|consumes?|consumed)\s+fewer\b"
    r"[^.!?]{0,100}?\b(?:tokens?|goal(?:-|\s+)tokens?(?:\s+use)?|costs?)\b"
    r"|"
    r"\b(?:requires?|required|needs?|needed)\s+fewer\b"
    r"[^.!?]{0,100}?\b(?:tokens?|goal(?:-|\s+)tokens?)\b"
    r"|"
    r"\b(?:improves?|improved|boosts?|boosted|enhances?|enhanced)\b"
    r"[^.!?]{0,100}?\btoken(?:-|\s+)efficiency\b"
    r"|"
    r"\b(?:better|greater|more)\s+token(?:\s+efficiency|(?:-|\s+)efficient)\b"
    r"|"
    r"\b(?:has|have|had)\s+(?:higher|better)\s+token(?:-|\s+)efficiency\b"
    r"|"
    r"\b(?:is|are|was|were)\s+cheaper\b"
    r"|"
    r"\bcosts?\s+less\b"
    r"|"
    r"\b(?:trims?|trimmed|minimizes?|minimized)\s+token(?:-|\s+)(?:use|usage)\b"
    r")",
    re.IGNORECASE,
)
CLAUSE_BOUNDARY_PATTERN = re.compile(
    r"[.!?;:][\"'”’»›\)\]]*"
    r"(?:\s+|(?=[\"'“‘«‹\(\[]*[^\W\d_])|$)"
    r"|\s*[–—]\s*"
    r"|,\s+(?!(?:and|or)\s+(?:may|can|could)\b)"
    r"(?=[^.!?;,]{0,100}\b(?:saves|saved|reduces|reduced|lowers|lowered|cuts|"
    r"uses\s+fewer|used\s+fewer|consumes\s+fewer|consumed\s+fewer|improves|improved|"
    r"requires\s+fewer|required\s+fewer|needs\s+fewer|needed\s+fewer|"
    r"boosts|boosted|enhances|enhanced|(?:better|greater|more)\s+token|"
    r"(?:has|have|had)\s+(?:higher|better)\s+token|(?:is|are|was|were)\s+cheaper|"
    r"costs?\s+less|trims|trimmed|minimizes|minimized|yields|yielded)\b)"
    r"|\s+(?:even\s+though|though)\s+"
    r"(?=[^.!?;:–—]{0,100}\b(?:saves|saved|reduces|reduced|lowers|lowered|cuts|"
    r"uses\s+fewer|used\s+fewer|consumes\s+fewer|consumed\s+fewer|improves|improved|"
    r"requires\s+fewer|required\s+fewer|needs\s+fewer|needed\s+fewer|"
    r"boosts|boosted|enhances|enhanced|(?:better|greater|more)\s+token|"
    r"(?:has|have|had)\s+(?:higher|better)\s+token|(?:is|are|was|were)\s+cheaper|"
    r"costs?\s+less|trims|trimmed|minimizes|minimized)\b)"
    r"|\s+(?:but|however|yet|nevertheless|while|whereas|although)\s+",
    re.IGNORECASE,
)
NEGATED_CLAIM_PATTERN = re.compile(
    r"\b(?:(?:do|does|did|is|are|was|were|can|could|will|would|has|have)\s+not|cannot)\b"
    r"|\bnot\s+(?:evidence|proof)\b"
    r"|\bnot\b[^.;]{0,60}\b(?:tokens?|goal-token|costs?)\b"
    r"|\bwithout\s+(?:evidence|proof)\b"
    r"|\bnever\s+(?:saves?|saved|reduces?|reduced|lowers?|lowered|cuts?|"
    r"uses?|used|consumes?|consumed|requires?|required|needs?|needed|improves?|improved|"
    r"boosts?|boosted|enhances?|enhanced|has|have|had|is|are|was|were|costs?|"
    r"trims?|trimmed|minimizes?|minimized|yields?|yielded)\b",
    re.IGNORECASE,
)
SCOPED_NEGATION_PATTERN = re.compile(
    r"\b(?:not|without)\s+(?:evidence|proof)(?:\s+that)?\b"
    r"|\b(?:(?:do|does|did|can|could|will|would|has|have)\s+not|cannot)\s+"
    r"(?:prove|show|demonstrate|establish|mean)\b",
    re.IGNORECASE,
)
AUXILIARY_NEGATION_PATTERN = re.compile(
    r"\b(?:(?:do|does|did|is|are|was|were|can|could|will|would|has|have)\s+not|cannot)\b",
    re.IGNORECASE,
)
CONDITIONAL_CLAIM_PATTERN = re.compile(
    r"\b(?:required to claim|allowed only|must pass)\b",
    re.IGNORECASE,
)
META_CLAIM_PREFIX_PATTERN = re.compile(
    r"\b(?:claim|assertion)\s+that\b",
    re.IGNORECASE,
)
HISTORICAL_CONTEXT_PATTERN = re.compile(
    r"\b(?:historical|pre-v2|retained dataset|negative baseline)\b",
    re.IGNORECASE,
)
HISTORICAL_FAILURE_PATTERN = re.compile(
    r"\b(?:failed|negative result|did not|does not|not evidence|not proof)\b",
    re.IGNORECASE,
)
METRIC_DEFINITION_PATTERN = re.compile(
    r"\b(?:positive|negative)\s+(?:paired\s+)?differences?\b"
    r"[^.!?]{0,100}\bmean\b",
    re.IGNORECASE,
)
LIMITED_RESULT_PATTERN = re.compile(
    r"\b(?:in\s+)?only\s+\d+\s+of\s+\d+\b",
    re.IGNORECASE,
)
NUMERIC_METRIC_ROW_PATTERN = re.compile(
    r"^\s*\|.*\|\s*[+-]?(?:\d[\d,.]*|\d*\.\d+)%?\s*\|\s*$"
)
LEADING_EVIDENCE_GATE_PATTERN = re.compile(
    r"^\s*(?:if|when|unless)\b"
    r"(?=[^.!?;,]{0,160}\b(?:evidence|proof|stud(?:y|ies)|tests?|results?)\b)"
    r"[^.!?;,]*$",
    re.IGNORECASE,
)
LEADING_EVIDENCE_GATE_PREFIX_PATTERN = re.compile(
    r"^\s*(?:if|when|unless)\b"
    r"(?=[^.!?;,]{0,160}\b(?:evidence|proof|stud(?:y|ies)|tests?|results?)\b)"
    r"[^.!?;,]*,\s*",
    re.IGNORECASE,
)
MODAL_CLAIM_PREFIX_PATTERN = re.compile(
    r"\b(?:may|can|could)\s*$",
    re.IGNORECASE,
)
PUSH_BRANCHES_PATTERN = re.compile(
    r"^  push:\s*$\n"
    r"(?:^    (?!branches:).*$\n)*"
    r"^    branches:\s*$\n"
    r"(?P<branches>(?:^      -\s+.+$\n?)+)",
    re.MULTILINE,
)


def git(*args: str, root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def assess_release_policy(root: Path) -> dict[str, object]:
    path = root / ".codex-plugin" / "release-policy.json"
    if not path.is_file():
        return {
            "valid": False,
            "path": str(path),
            "error": "release policy is missing",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "path": str(path), "error": f"invalid release policy: {exc}"}
    valid = (
        isinstance(value, dict)
        and set(value) == RELEASE_POLICY_KEYS
        and value.get("schema_version") == 1
        and value.get("release_mode") == "experimental_non_claim"
        and value.get("token_efficiency_claim") is False
        and value.get("cost_savings_claim") is False
    )
    return {
        "valid": valid,
        "path": str(path),
        "error": None if valid else "release policy schema or values are invalid",
    }


def assess_ci_validation_workflow(root: Path) -> str | None:
    path = root / ".github" / "workflows" / "validate.yml"
    if not path.is_file():
        return "CI validation workflow is missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "CI validation workflow is unreadable"
    match = PUSH_BRANCHES_PATTERN.search(text)
    if match is None:
        return "CI validation workflow must run on pushes to exactly main"
    branches = [
        line.split("-", maxsplit=1)[1].strip().strip("'\"")
        for line in match.group("branches").splitlines()
    ]
    if branches != ["main"]:
        return "CI validation workflow must run on pushes to exactly main"
    return None


def _claim_offsets(text: str) -> Iterator[int]:
    fragment_start = 0
    pending_evidence_gate = False
    boundaries = CLAUSE_BOUNDARY_PATTERN.finditer(text)
    for boundary in (*boundaries, None):
        fragment_end = len(text) if boundary is None else boundary.start()
        fragment = text[fragment_start:fragment_end]
        claim_matches = tuple(POSITIVE_CLAIM_PATTERN.finditer(fragment))
        inline_evidence_gate = LEADING_EVIDENCE_GATE_PREFIX_PATTERN.match(fragment)
        modal_prefix_start = (
            inline_evidence_gate.end() if inline_evidence_gate is not None else 0
        )
        governed_prefix = (
            fragment[modal_prefix_start:claim_matches[0].start()]
            if claim_matches
            else ""
        )
        modal_evidence_gate = (
            (pending_evidence_gate or inline_evidence_gate is not None)
            and bool(claim_matches)
            and not re.search(
                r"[,;.!?]|\b(?:but|however|yet|nevertheless|while|whereas|although)\b",
                governed_prefix,
                re.IGNORECASE,
            )
            and MODAL_CLAIM_PREFIX_PATTERN.search(governed_prefix) is not None
        )
        scope_starts: list[int] = []
        for claim_index, claim_match in enumerate(claim_matches):
            scope_start = 0
            coordinators = tuple(
                re.finditer(r"\b(?:and|or)\b", fragment[: claim_match.start()], re.IGNORECASE)
            )
            if coordinators:
                coordinator = coordinators[-1]
                words_before_claim = fragment[coordinator.end():claim_match.start()].strip()
                claim_verb = claim_match.group(0).split(maxsplit=1)[0].casefold()
                finite_claim_verb = claim_verb in {
                    "saves",
                    "saved",
                    "reduces",
                    "reduced",
                    "lowers",
                    "lowered",
                    "cuts",
                    "uses",
                    "used",
                    "consumes",
                    "consumed",
                    "requires",
                    "required",
                    "needs",
                    "needed",
                    "improves",
                    "improved",
                    "boosts",
                    "boosted",
                    "enhances",
                    "enhanced",
                    "has",
                    "have",
                    "had",
                    "is",
                    "are",
                    "was",
                    "were",
                    "costs",
                    "trims",
                    "trimmed",
                    "minimizes",
                    "minimized",
                    "yields",
                    "yielded",
                }
                prior_context = fragment[:coordinator.start()]
                finite_after_scoped_prefix = (
                    coordinator.group(0).casefold() == "and"
                    and finite_claim_verb
                    and (
                        AUXILIARY_NEGATION_PATTERN.search(prior_context) is not None
                        or HISTORICAL_FAILURE_PATTERN.search(prior_context) is not None
                    )
                )
                modal_continuation = (
                    modal_evidence_gate
                    and claim_index > 0
                    and re.fullmatch(
                        r"\s*(?:(?:use|usage)\s*)?,?\s*"
                        r"(?:and|or)\s+(?:may|can|could)\s*",
                        fragment[
                            claim_matches[claim_index - 1].end():claim_match.start()
                        ],
                        re.IGNORECASE,
                    )
                    is not None
                )
                has_independent_scope = (
                    (bool(words_before_claim) and not modal_continuation)
                    or (
                        SCOPED_NEGATION_PATTERN.search(
                            fragment[: claim_match.end()]
                        ) is None
                        and finite_after_scoped_prefix
                    )
                )
                if has_independent_scope:
                    scope_start = coordinator.end()
            scope_starts.append(scope_start)

        for index, claim_match in enumerate(claim_matches):
            scope_start = scope_starts[index]
            later_starts = (
                start
                for start in scope_starts[index + 1:]
                if start > claim_match.end()
            )
            scope_end = min(later_starts, default=len(fragment))
            post_claim_context = fragment[claim_match.end():scope_end]
            qualifier_coordinator = re.search(
                r"\b(?:and|or)\b", post_claim_context, re.IGNORECASE
            )
            qualifier_end = (
                claim_match.end() + qualifier_coordinator.start()
                if qualifier_coordinator is not None
                else scope_end
            )
            qualifier_context = fragment[scope_start:qualifier_end]
            preclaim_context = fragment[scope_start:claim_match.end()]
            absolute_claim_start = fragment_start + claim_match.start()
            conditional_nonclaim = (
                CONDITIONAL_CLAIM_PATTERN.search(preclaim_context) is not None
                or (
                    META_CLAIM_PREFIX_PATTERN.search(preclaim_context) is not None
                    and CONDITIONAL_CLAIM_PATTERN.search(qualifier_context) is not None
                )
            )
            historical_failure_nonclaim = (
                HISTORICAL_CONTEXT_PATTERN.search(
                    fragment[scope_start:claim_match.start()]
                )
                is not None
                and HISTORICAL_FAILURE_PATTERN.search(
                    fragment[claim_match.end():qualifier_end]
                )
                is not None
            )
            is_historical_limited_result = (
                HISTORICAL_CONTEXT_PATTERN.search(text[:absolute_claim_start]) is not None
                and LIMITED_RESULT_PATTERN.search(qualifier_context) is not None
            )
            if (
                not (modal_evidence_gate and scope_start == 0)
                and NEGATED_CLAIM_PATTERN.search(preclaim_context) is None
                and not conditional_nonclaim
                and not historical_failure_nonclaim
                and METRIC_DEFINITION_PATTERN.search(preclaim_context) is None
                and not is_historical_limited_result
            ):
                yield fragment_start + claim_match.start()
        if boundary is not None:
            pending_evidence_gate = (
                boundary.group(0).lstrip().startswith(",")
                and LEADING_EVIDENCE_GATE_PATTERN.fullmatch(fragment) is not None
            )
            fragment_start = boundary.end()


def _contains_positive_claim(text: str) -> bool:
    return next(_claim_offsets(text), None) is not None


def _markdown_blocks(text: str) -> Iterator[tuple[int, str]]:
    block_start = 0
    for separator in re.finditer(r"\n[ \t]*\n+", text):
        yield block_start, text[block_start:separator.start()]
        block_start = separator.end()
    yield block_start, text[block_start:]


def _json_string_values(
    value: object,
    locator: str = "$",
) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield locator, value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _json_string_values(child, f"{locator}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _json_string_values(child, f"{locator}[{index}]")


def scan_positive_public_claims(root: Path) -> list[str]:
    excluded_parts = {".git", ".agents", ".omx", "__pycache__"}
    paths = sorted(
        {
            path
            for pattern in PUBLIC_TEXT_PATTERNS
            for path in root.glob(pattern)
            if not excluded_parts.intersection(path.relative_to(root).parts)
        }
    )
    findings: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            document = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for block_start, block in _markdown_blocks(document):
            lines = block.splitlines(keepends=True)
            is_table = bool(lines) and all(
                not line.strip() or line.lstrip().startswith("|") for line in lines
            )
            units: list[tuple[int, str]] = [(block_start, block)]
            if is_table:
                units = []
                line_start = block_start
                for line in lines:
                    content = line.rstrip("\r\n")
                    if not NUMERIC_METRIC_ROW_PATTERN.fullmatch(content):
                        units.append((line_start, content))
                    line_start += len(line)
            for unit_start, unit in units:
                for claim_offset in _claim_offsets(unit):
                    line_number = document.count("\n", 0, unit_start + claim_offset) + 1
                    findings.append(f"{path.relative_to(root)}:{line_number}")

    manifest_path = root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = None
    for locator, value in _json_string_values(manifest):
        if _contains_positive_claim(value):
            findings.append(f".codex-plugin/plugin.json:{locator}")
    return findings


def assess_live_hooks_trust(root: Path) -> dict[str, object]:
    path = root / "artifacts" / "metrics" / "live-hooks-trust.json"
    if not path.is_file():
        return {
            "ready": False,
            "path": str(path),
            "error": "live /hooks load and trust evidence is unavailable",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ready": False, "path": str(path), "error": f"invalid JSON: {exc}"}
    expected_keys = {
        "schema_version",
        "evidence_type",
        "checked_via",
        "checked_at",
        "loaded",
        "trusted",
        "hook_events",
        "plugin_version",
        "hooks_json_sha256",
        "adapter_sha256s",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        return {
            "ready": False,
            "path": str(path),
            "error": "live /hooks evidence schema is invalid",
        }
    required_events = {"UserPromptSubmit", "PreToolUse", "PreCompact", "Stop"}
    checked_at = value.get("checked_at")
    try:
        parsed_checked_at = dt.datetime.fromisoformat(
            str(checked_at).replace("Z", "+00:00")
        )
    except ValueError:
        parsed_checked_at = None
    timestamp_fresh = False
    if parsed_checked_at is not None and parsed_checked_at.tzinfo is not None:
        age = dt.datetime.now(dt.timezone.utc) - parsed_checked_at.astimezone(
            dt.timezone.utc
        )
        timestamp_fresh = -dt.timedelta(minutes=5) <= age <= dt.timedelta(hours=24)

    manifest_path = root / ".codex-plugin" / "plugin.json"
    hooks_path = root / "hooks" / "hooks.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_hooks_sha256 = hashlib.sha256(hooks_path.read_bytes()).hexdigest()
        expected_adapter_sha256s = {
            adapter: hashlib.sha256((root / adapter).read_bytes()).hexdigest()
            for adapter in LIVE_HOOK_ADAPTERS
        }
    except (OSError, json.JSONDecodeError):
        manifest = {}
        expected_hooks_sha256 = None
        expected_adapter_sha256s = None
    expected_version = manifest.get("version") if isinstance(manifest, dict) else None
    ready = (
        value.get("schema_version") == 1
        and value.get("evidence_type") == "codex_live_hooks_trust"
        and value.get("checked_via") == "/hooks"
        and timestamp_fresh
        and value.get("loaded") is True
        and value.get("trusted") is True
        and isinstance(value.get("hook_events"), list)
        and set(value.get("hook_events", [])) == required_events
        and value.get("plugin_version") == expected_version
        and value.get("hooks_json_sha256") == expected_hooks_sha256
        and value.get("adapter_sha256s") == expected_adapter_sha256s
    )
    return {
        "ready": ready,
        "path": str(path),
        "error": None if ready else "live /hooks evidence does not prove all hooks loaded and trusted",
    }


def assess(root: Path = ROOT) -> dict[str, object]:
    blockers: list[str] = []
    warnings: list[str] = []

    if git("rev-parse", "--verify", "HEAD", root=root).returncode != 0:
        blockers.append("repository has no commit")

    remote = git("remote", "get-url", "origin", root=root)
    if remote.returncode != 0 or not remote.stdout.strip():
        blockers.append("origin remote is not configured")

    licenses = [path for path in root.glob("LICENSE*") if path.is_file()]
    if not licenses:
        blockers.append("no license file exists")

    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest: dict[str, object] = {}
    if not manifest_path.is_file():
        blockers.append("plugin manifest is missing")
    else:
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append(f"plugin manifest is invalid JSON: {exc}")
        else:
            if not isinstance(value, dict):
                blockers.append("plugin manifest is invalid JSON: expected an object")
            else:
                manifest = value

    if manifest:
        if not manifest.get("license"):
            blockers.append("plugin manifest has no license identifier")
        if not manifest.get("repository"):
            blockers.append("plugin manifest has no repository URL")
        else:
            repository_name = Path(urlparse(str(manifest["repository"])).path).name.removesuffix(".git")
            if repository_name != manifest.get("name"):
                warnings.append(
                    "repository URL still uses the pre-migration remote name; update it only "
                    "after the Relay repository exists"
                )

    skill_path = root / "skills" / "relay" / "SKILL.md"
    if not skill_path.is_file():
        blockers.append("relay skill source is missing")
    elif "name: relay" not in skill_path.read_text(encoding="utf-8"):
        blockers.append("skill frontmatter name is not relay")

    if manifest.get("name") != "relay":
        blockers.append("public plugin name is not Relay")

    status = git("status", "--porcelain=v1", "-uall", root=root)
    if status.stdout.strip():
        blockers.append("working tree is not clean")

    workflow_error = assess_ci_validation_workflow(root)
    if workflow_error is not None:
        blockers.append(workflow_error)

    if not (root / "hooks" / "hooks.json").is_file():
        blockers.append("plugin hook manifest is missing")

    live_hooks = assess_live_hooks_trust(root)
    release_policy = assess_release_policy(root)
    positive_claims = scan_positive_public_claims(root)

    if not release_policy["valid"]:
        blockers.append(f"release policy invalid: {release_policy['error']}")
    if positive_claims:
        blockers.append(
            "positive token/cost claim appears on public release surface: "
            + ", ".join(positive_claims)
        )
    if not live_hooks["ready"]:
        warnings.append(str(live_hooks["error"]))

    non_claim = bool(release_policy["valid"]) and not positive_claims
    release_mode = "experimental_non_claim" if non_claim else "claiming_or_unspecified"
    if non_claim:
        warnings.append("release contains no token- or cost-saving claims")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "release_mode": release_mode,
        "live_hooks_trust": live_hooks,
        "release_policy": release_policy,
        "positive_claims": positive_claims,
    }


def main() -> int:
    result = assess()
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
