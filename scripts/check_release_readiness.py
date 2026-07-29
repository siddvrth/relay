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
sys.path.insert(
    0,
    str(ROOT / "skills" / "relay" / "scripts"),
)


RELEASE_POLICY_KEYS = {
    "schema_version",
    "release_mode",
    "token_efficiency_claim",
    "cost_savings_claim",
}
LIVE_HOOK_ADAPTERS = (
    "hooks/relay_hook.sh",
    "codex/relay_hook.sh",
    "scripts/workflow/relay_hook.sh",
)
# Sort order is part of the digest; do not reorder.
RUNTIME_BINDING_PATHS = tuple(
    sorted(
        (
            ".codex-plugin/plugin.json",
            "skills/relay/SKILL.md",
            "skills/relay/reference.md",
            "skills/relay/examples.md",
            "skills/relay/agents/openai.yaml",
            "skills/relay/scripts/write_handoff.py",
            "skills/relay/scripts/context_handoff.py",
            "skills/relay/scripts/context_usage.py",
            "skills/relay/scripts/codex_app_delivery_state.py",
            "skills/relay/scripts/codex_app_jsonrpc.py",
            "skills/relay/scripts/codex_app_protocol.py",
            "skills/relay/scripts/codex_app_transport.py",
            "skills/relay/scripts/codex_app_worker.py",
            "skills/relay/scripts/transfer_control.py",
            "skills/relay/scripts/goal_telemetry_report.py",
            "skills/relay/scripts/goal_telemetry_v3.py",
            "skills/relay/scripts/goal_telemetry_v3_contract.py",
            "skills/relay/scripts/goal_telemetry_v3_report.py",
            "skills/relay/scripts/goal_telemetry_v3_schema.py",
            "scripts/check_release_readiness.py",
            "scripts/validate_distribution.py",
            "hooks/hooks.json",
            *LIVE_HOOK_ADAPTERS,
            "install.sh",
            "audit_install.sh",
            "repair_active_install.sh",
            "completion_gate.sh",
        )
    )
)
PREREGISTRATION_ARTIFACTS = (
    "task_set",
    "randomization_plan",
    "rubric",
    "analysis_plan",
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
from goal_telemetry_report import (  # noqa: E402
    V2_SCHEMA_VERSION,
    V2_STUDY_TYPE,
    V3_SCHEMA_VERSION,
    V3_STUDY_TYPE,
    build_v2_report,
    build_v3_report,
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


def experimental_non_claim_release(root: Path) -> bool:
    return bool(assess_release_policy(root)["valid"]) and not scan_positive_public_claims(root)


def _git_blob(root: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"{path} is unavailable at commit {revision}")
    return result.stdout


def _git_regular_blob(
    root: Path,
    revision: str,
    path: str,
    label: str,
) -> tuple[str, bytes]:
    entry = git("ls-tree", revision, "--", path, root=root)
    fields = entry.stdout.split(maxsplit=3) if entry.returncode == 0 else []
    if len(fields) != 4 or fields[0] not in {"100644", "100755"} or fields[1] != "blob":
        raise ValueError(f"{label} {path} is not a regular tracked blob")
    return fields[0], _git_blob(root, revision, path)


def _digest_runtime_entries(entries: Iterator[tuple[str, str, bytes]]) -> str:
    digest = hashlib.sha256(b"relay-runtime-binding-v2\0")
    for path, mode, content in entries:
        encoded_path = path.encode("utf-8")
        digest.update(encoded_path)
        digest.update(b"\0")
        digest.update(mode.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def runtime_digest_at_commit(root: Path, commit: str) -> str:
    def entry(path: str) -> tuple[str, str, bytes]:
        mode, content = _git_regular_blob(root, commit, path, "runtime file")
        return path, mode, content

    return _digest_runtime_entries(
        entry(path) for path in RUNTIME_BINDING_PATHS
    )


def current_runtime_digest(root: Path) -> str:
    entries: list[tuple[str, str, bytes]] = []
    for relative in RUNTIME_BINDING_PATHS:
        path = root / relative
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"current runtime file {relative} is not a regular file")
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"current runtime file {relative} is unavailable: {exc}") from exc
        entries.append((relative, mode, content))
    return _digest_runtime_entries(iter(entries))


def _resolved_commit(root: Path, declared: object, label: str) -> str:
    result = git("rev-parse", "--verify", f"{declared}^{{commit}}", root=root)
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", result.stdout.strip()):
        raise ValueError(f"{label} does not resolve to a real commit")
    return result.stdout.strip()


def _require_ancestor(root: Path, ancestor: str, descendant: str, label: str) -> None:
    result = git("merge-base", "--is-ancestor", ancestor, descendant, root=root)
    if result.returncode != 0:
        raise ValueError(label)


def _validate_evidence_binding(
    root: Path,
    document: dict[str, object],
) -> dict[str, str]:
    control = _resolved_commit(root, document["control_commit_id"], "control_commit_id")
    candidate = _resolved_commit(root, document["candidate_commit_id"], "candidate_commit_id")
    head = _resolved_commit(root, "HEAD", "release HEAD")
    _require_ancestor(
        root,
        control,
        candidate,
        "control commit is not an ancestor of candidate commit",
    )
    _require_ancestor(
        root,
        candidate,
        head,
        "candidate commit is not an ancestor of release HEAD",
    )
    origin = git("remote", "get-url", "origin", root=root)
    if origin.returncode != 0 or document["repository"] != origin.stdout.strip():
        raise ValueError("study repository does not match the release origin remote")

    control_digest = runtime_digest_at_commit(root, control)
    candidate_digest = runtime_digest_at_commit(root, candidate)
    release_digest = current_runtime_digest(root)
    if document["control_runtime_sha256"] != control_digest:
        raise ValueError("declared control runtime digest does not match control commit")
    if document["candidate_runtime_sha256"] != candidate_digest:
        raise ValueError("declared candidate runtime digest does not match candidate commit")
    if release_digest != candidate_digest:
        raise ValueError("release runtime has drifted from the candidate commit")

    status = git("status", "--porcelain=v1", "-uall", root=root)
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError("release working tree must be clean for evidence binding")

    preregistration = document["preregistration"]
    if not isinstance(preregistration, dict):
        raise ValueError("preregistration must be an object")
    for stem in PREREGISTRATION_ARTIFACTS:
        path = str(preregistration[f"{stem}_path"])
        expected_digest = str(preregistration[f"{stem}_sha256"])
        _, candidate_content = _git_regular_blob(
            root, candidate, path, "preregistration artifact"
        )
        _, head_content = _git_regular_blob(
            root, head, path, "preregistration artifact"
        )
        tracked = git("ls-files", "--error-unmatch", "--", path, root=root)
        if tracked.returncode != 0:
            raise ValueError(f"preregistration artifact {path} is not tracked at release HEAD")
        try:
            current_path = root / path
            if current_path.is_symlink() or not current_path.is_file():
                raise ValueError(
                    f"preregistration artifact {path} is not a regular current file"
                )
            current_content = current_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"preregistration artifact {path} is unavailable: {exc}") from exc
        candidate_artifact_digest = hashlib.sha256(candidate_content).hexdigest()
        current_artifact_digest = hashlib.sha256(current_content).hexdigest()
        if candidate_artifact_digest != expected_digest:
            raise ValueError(
                f"preregistration artifact {path} hash does not match candidate commit"
            )
        if current_artifact_digest != expected_digest:
            raise ValueError(
                f"preregistration artifact {path} hash does not match current release tree"
            )
        if hashlib.sha256(head_content).hexdigest() != expected_digest:
            raise ValueError(
                f"preregistration artifact {path} hash does not match release HEAD"
            )

    return {
        "control_commit": control,
        "candidate_commit": candidate,
        "release_head": head,
        "control_runtime_sha256": control_digest,
        "candidate_runtime_sha256": candidate_digest,
        "release_runtime_sha256": release_digest,
        "repository_origin": origin.stdout.strip(),
    }


def assess_empirical_evidence(root: Path) -> dict[str, object]:
    metrics_root = root / "artifacts" / "metrics"
    candidates: list[str] = []
    v2_files: list[str] = []
    v3_files: list[str] = []
    valid_v2_reports: list[dict[str, object]] = []
    valid_v3_reports: list[dict[str, object]] = []
    passing_v3_reports: list[dict[str, object]] = []
    verified_bindings: list[dict[str, str]] = []
    v2_verified_bindings: list[dict[str, str]] = []
    errors: list[str] = []
    if metrics_root.is_dir():
        for path in sorted(metrics_root.glob("*.json")):
            if path.name == "live-hooks-trust.json":
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: invalid JSON: {exc}")
                continue
            if not isinstance(value, dict):
                continue
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
            candidates.append(path.name)
            if v2_marker:
                v2_files.append(path.name)
            if v3_marker:
                v3_files.append(path.name)
            if identity not in {
                (V2_SCHEMA_VERSION, V2_STUDY_TYPE),
                (V3_SCHEMA_VERSION, V3_STUDY_TYPE),
            }:
                errors.append(
                    f"{path.name}: mixed or partial telemetry schema/type markers are invalid"
                )
                continue
            try:
                report = (
                    build_v2_report(value)
                    if identity == (V2_SCHEMA_VERSION, V2_STUDY_TYPE)
                    else build_v3_report(value)
                )
                binding = _validate_evidence_binding(root, value)
            except ValueError as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if identity == (V2_SCHEMA_VERSION, V2_STUDY_TYPE):
                valid_v2_reports.append(report)
                v2_verified_bindings.append(binding)
            else:
                valid_v3_reports.append(report)
                if report["empirical_gate_passed"]:
                    passing_v3_reports.append(report)
                    verified_bindings.append(binding)

    gate_passed = bool(passing_v3_reports) and not errors
    return {
        "gate_passed": gate_passed,
        "candidate_files": candidates,
        "v2_prior_schema_files": v2_files,
        "v3_candidate_files": v3_files,
        "valid_v2_study_count": len(valid_v2_reports),
        "valid_v3_study_count": len(valid_v3_reports),
        "valid_passing_v3_study_count": len(passing_v3_reports),
        "valid_passing_study_count": len(passing_v3_reports),
        "verified_bindings": verified_bindings,
        "v2_verified_bindings": v2_verified_bindings,
        "errors": errors,
        "historical_aggregate_qualifies": False,
    }


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

    if not (root / ".agents" / "plugins" / "marketplace.json").is_file():
        warnings.append(
            "no repository marketplace catalog; direct install.sh works, but Git-backed "
            "plugin marketplace installation is not yet available"
        )

    warnings.append(
        "the 30% threshold is an experimental safety policy; realistic multi-step threshold "
        "calibration is still required before a non-beta effectiveness claim"
    )

    empirical = assess_empirical_evidence(root)
    live_hooks = assess_live_hooks_trust(root)
    release_policy = assess_release_policy(root)
    positive_claims = scan_positive_public_claims(root)
    token_claim_blockers: list[str] = []
    v3_candidate_files = empirical.get("v3_candidate_files")
    empirical_errors = empirical.get("errors")
    empirical_gate_passed = empirical.get("gate_passed") is True
    if not isinstance(v3_candidate_files, list) or not v3_candidate_files:
        token_claim_blockers.append(
            "v3 acknowledgement-gated evidence is absent; V2 is prior-schema only"
        )
    elif isinstance(empirical_errors, list) and empirical_errors:
        token_claim_blockers.append(
            "v3 acknowledgement-gated evidence is invalid: "
            + "; ".join(str(error) for error in empirical_errors)
        )
    elif not empirical_gate_passed:
        token_claim_blockers.append(
            "v3 acknowledgement-gated evidence does not pass chain, quality, and token-direction gates"
        )
    if not live_hooks["ready"]:
        token_claim_blockers.append(str(live_hooks["error"]))

    cost_claim_blockers = [
        "exact usage-category telemetry is unavailable for a cost claim"
    ]
    claim_blockers = [*token_claim_blockers, *cost_claim_blockers]
    token_efficiency_claim_ready = not token_claim_blockers
    cost_claim_ready = not cost_claim_blockers
    non_claim = bool(release_policy["valid"]) and not positive_claims
    release_mode = "experimental_non_claim" if non_claim else "claiming_or_unspecified"
    if not release_policy["valid"]:
        blockers.append(f"release policy invalid: {release_policy['error']}")
    if positive_claims:
        blockers.append(
            "positive token/cost claim appears on public release surface: "
            + ", ".join(positive_claims)
        )
    if not non_claim:
        blockers.extend(f"token/cost claim blocked: {item}" for item in claim_blockers)
    else:
        warnings.append(
            "release remains experimental and makes no token- or cost-saving claim; "
            "claim-only evidence blockers do not block package publication"
        )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "release_mode": release_mode,
        "token_efficiency_claim_ready": token_efficiency_claim_ready,
        "cost_claim_ready": cost_claim_ready,
        "token_cost_claim_ready": (
            token_efficiency_claim_ready and cost_claim_ready
        ),
        "claim_blockers": claim_blockers,
        "empirical_evidence": empirical,
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
