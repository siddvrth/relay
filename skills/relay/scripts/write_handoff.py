#!/usr/bin/env python3
"""Write a bounded, self-contained relay capsule."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import datetime as dt
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transfer_control import (  # noqa: E402
    TransferError,
    authority_transaction,
    derive_transfer_id,
    require_authority_transaction,
)


DEFAULT_STATUS_LINES = 120
DEFAULT_DIFF_LINES = 120
DEFAULT_HANDOFF_TRIGGER_RATIO = 0.30
DEFAULT_CAPSULE_BUDGET_BYTES = 4096
DEFAULT_PROMPT_BUDGET_BYTES = 1024
MAX_CAPSULE_BUDGET_BYTES = DEFAULT_CAPSULE_BUDGET_BYTES
MAX_PROMPT_BUDGET_BYTES = DEFAULT_PROMPT_BUDGET_BYTES
CAPSULE_VERSION = 2
ACTIVE_TASK_NAME = ".active-task.json"
STATE_DIR_NAME = "relay"

CRITICAL_FIELDS = (
    "session_id",
    "revision",
    "transfer_id",
    "goal_identity",
    "transfer_nonce",
    "objective",
    "active_task",
    "phase",
    "status",
    "completion_criteria",
    "remaining_work",
    "constraints",
    "authoritative_files",
    "resume_validation",
    "next_action",
)
OPTIONAL_SECTIONS = (
    ("completed_work", "Completed Work"),
    ("decisions", "Decisions"),
    ("blockers", "Blockers / Risks"),
    ("validation_evidence", "Validation Evidence"),
)

PLACEHOLDERS = {
    "",
    "-",
    "?",
    "tbd",
    "todo",
    "unknown",
    "n/a",
    "na",
    "none",
    "(none)",
    "not recorded",
    "not yet recorded",
}
_OPEN_AUTHORITIES: list[ExitStack] = []


def _close_authority(stack: ExitStack) -> None:
    try:
        stack.close()
    finally:
        if stack in _OPEN_AUTHORITIES:
            _OPEN_AUTHORITIES.remove(stack)


def byte_budget_limit_error(
    capsule_budget_bytes: int,
    prompt_budget_bytes: int,
) -> str | None:
    """Return the canonical hard-cap violation, if any."""

    if capsule_budget_bytes > MAX_CAPSULE_BUDGET_BYTES:
        return (
            "capsule byte budget cannot exceed "
            f"{MAX_CAPSULE_BUDGET_BYTES} bytes"
        )
    if prompt_budget_bytes > MAX_PROMPT_BUDGET_BYTES:
        return (
            "prompt byte budget cannot exceed "
            f"{MAX_PROMPT_BUDGET_BYTES} bytes"
        )
    return None


def run(command: Sequence[str], cwd: Path) -> tuple[int, str, str]:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def resolve_repo(path: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    code, stdout, _stderr = run(["git", "rev-parse", "--show-toplevel"], candidate)
    if code == 0 and stdout:
        return Path(stdout).resolve()
    return candidate


def git(repo: Path, args: Sequence[str]) -> str:
    code, stdout, stderr = run(["git", *args], repo)
    if code != 0:
        message = stderr or stdout or f"git {' '.join(args)} failed"
        return f"(unavailable: {message})"
    return stdout or "(none)"


def trim_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - max_lines
    retained = "\n".join(lines[:max_lines])
    return f"{retained}\n... ({omitted} more lines omitted)"


def parse_ratio(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        text = str(value).strip().lower()
        if not text or text in {"unknown", "n/a", "na"}:
            return None
        if text.endswith("%"):
            parsed = float(text[:-1].strip()) / 100
        else:
            parsed = float(text)
            parsed = parsed / 100 if parsed > 1 else parsed
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0 <= parsed <= 1 or parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def derive_goal_identity(explicit: str, goal_objective: str, objective: str) -> str:
    if explicit.strip():
        return explicit.strip()
    basis_label = "goal" if goal_objective.strip() else "task"
    basis = goal_objective.strip() or objective.strip()
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"{basis_label}:sha256:{digest}"


def session_scope(session_id: str | None) -> str:
    stable = (session_id or "default").strip() or "default"
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def state_root(repo: Path) -> Path:
    return repo / ".omx" / "state" / STATE_DIR_NAME


def session_state_dir(repo: Path, session_id: str | None) -> Path:
    return state_root(repo) / "sessions" / session_scope(session_id)


def active_task_path(repo: Path, session_id: str | None = None) -> Path:
    return session_state_dir(repo, session_id) / ACTIVE_TASK_NAME


def atomic_write_text(path: Path, content: str) -> None:
    require_authority_transaction()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def save_active_task(
    repo: Path,
    *,
    session_id: str = "",
    objective: str,
    next_action: str,
    goal_objective: str = "",
    capsule_path: str | None = None,
    reason: str = "",
    active_task: str = "",
    phase: str = "",
    status: str = "",
    completion_criteria: Sequence[str] = (),
    completed_work: Sequence[str] = (),
    remaining_work: Sequence[str] = (),
    constraints: Sequence[str] = (),
    decisions: Sequence[str] = (),
    blockers: Sequence[str] = (),
    authoritative_files: Sequence[str] = (),
    validation_evidence: Sequence[str] = (),
    resume_validation_command: str = "",
    resume_validation_expected: str = "",
    revision: int = 0,
    goal_identity: str = "",
    transfer_nonce: str = "",
    transfer_id: str = "",
) -> Path:
    path = active_task_path(repo, session_id)
    payload: dict[str, Any] = {
        "version": CAPSULE_VERSION,
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_id": session_id,
        "objective": objective,
        "active_task": active_task,
        "phase": phase,
        "status": status,
        "completion_criteria": list(completion_criteria),
        "completed_work": list(completed_work),
        "remaining_work": list(remaining_work),
        "constraints": list(constraints),
        "decisions": list(decisions),
        "blockers": list(blockers),
        "authoritative_files": list(authoritative_files),
        "validation_evidence": list(validation_evidence),
        "resume_validation": {
            "command": resume_validation_command,
            "expected": resume_validation_expected,
        },
        "next_action": next_action,
        "goal_objective": goal_objective,
        "capsule_path": capsule_path,
        "reason": reason,
        "revision": revision,
        "goal_identity": goal_identity,
        "transfer_nonce": transfer_nonce,
        "transfer_id": transfer_id,
    }
    atomic_write_json(path, payload)
    return path


def load_active_task(repo: Path, session_id: str | None = None) -> dict[str, Any]:
    path = active_task_path(repo, session_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def should_persist_active_task(
    repo: Path,
    out_path: Path,
    update_only: bool,
    session_id: str | None = None,
) -> bool:
    if update_only:
        return True
    try:
        return out_path.resolve().parent == session_state_dir(repo, session_id).resolve()
    except OSError:
        return False


def default_out_path(
    repo: Path,
    now: dt.datetime,
    session_id: str | None = None,
    revision: int | None = None,
) -> Path:
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    revision_suffix = f"-r{revision}" if revision is not None else ""
    root = session_state_dir(repo, session_id)
    return root / f"{stamp}{revision_suffix}-handoff.md"


def _append_argument(
    parser: argparse.ArgumentParser,
    *flags: str,
    dest: str,
    help_text: str,
) -> None:
    parser.add_argument(*flags, dest=dest, action="append", default=[], help=help_text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a bounded fresh-session checkpoint capsule."
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--out")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--source-session-id", default="")
    parser.add_argument("--goal-identity", default="")
    parser.add_argument("--transfer-nonce", default="")
    parser.add_argument(
        "--objective",
        default=os.environ.get("RELAY_OBJECTIVE", ""),
    )
    parser.add_argument("--active-task", default="")
    parser.add_argument("--phase", default="")
    parser.add_argument("--status", default="")
    _append_argument(
        parser,
        "--completion-criteria",
        dest="completion_criteria",
        help_text="Completion criterion. Repeat when needed.",
    )
    _append_argument(
        parser,
        "--completed-work",
        "--completed",
        dest="completed_work",
        help_text="Completed work item.",
    )
    _append_argument(
        parser,
        "--remaining-work",
        "--remaining",
        dest="remaining_work",
        help_text="Remaining work item.",
    )
    _append_argument(
        parser,
        "--constraints",
        "--constraint",
        dest="constraints",
        help_text="Constraint or non-goal.",
    )
    parser.add_argument(
        "--next-step",
        "--next-action",
        dest="next_step",
        default=os.environ.get("RELAY_NEXT_STEP", ""),
    )
    parser.add_argument("--goal-objective", default="")
    parser.add_argument("--reason", default="context checkpoint")
    parser.add_argument("--context-used")
    parser.add_argument("--handoff-threshold", type=float, default=DEFAULT_HANDOFF_TRIGGER_RATIO)
    parser.add_argument("--force-handoff", action="store_true")
    parser.add_argument("--context-remaining")
    _append_argument(parser, "--note", dest="note", help_text="Optional note.")
    _append_argument(
        parser,
        "--decisions",
        "--decision",
        dest="decisions",
        help_text="Decision and rationale.",
    )
    _append_argument(
        parser,
        "--blockers",
        "--blocker",
        dest="blockers",
        help_text="Blocker or risk.",
    )
    _append_argument(
        parser,
        "--validation-evidence",
        "--validation-status",
        dest="validation_evidence",
        help_text="Historical validation evidence. Repeat when needed.",
    )
    parser.add_argument("--resume-validation-command", default="")
    parser.add_argument("--resume-validation-expected", default="")
    _append_argument(
        parser,
        "--authoritative-files",
        "--authoritative-file",
        "--files-touched",
        "--file-touched",
        dest="authoritative_files",
        help_text="Exact authoritative file or symbol.",
    )
    _append_argument(
        parser,
        "--commands-run",
        "--command-run",
        dest="commands_run",
        help_text="Validation command already run.",
    )
    parser.add_argument("--max-status-lines", type=int, default=DEFAULT_STATUS_LINES)
    parser.add_argument("--max-diff-lines", type=int, default=DEFAULT_DIFF_LINES)
    parser.add_argument(
        "--capsule-budget-bytes",
        type=int,
        default=DEFAULT_CAPSULE_BUDGET_BYTES,
    )
    parser.add_argument(
        "--prompt-budget-bytes",
        type=int,
        default=DEFAULT_PROMPT_BUDGET_BYTES,
    )
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--emit-json", action="store_true")
    parser.add_argument("--update-active-task-only", action="store_true")
    return parser.parse_args()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def validate_structural_readiness(
    state: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    failures: list[str] = []
    for field in CRITICAL_FIELDS:
        value = state.get(field)
        if field == "revision":
            if type(value) is not int or value < 1:
                failures.append("missing:revision")
            continue
        if field in {
            "completion_criteria",
            "remaining_work",
            "constraints",
            "authoritative_files",
        }:
            values = _as_list(value)
            if not values:
                failures.append(f"missing:{field}")
            elif any(_normalized_text(item) in PLACEHOLDERS for item in values):
                failures.append(f"placeholder:{field}")
            continue
        if field == "resume_validation":
            resume_validation = value if isinstance(value, dict) else {}
            for key in ("command", "expected"):
                normalized = _normalized_text(resume_validation.get(key, ""))
                if not normalized:
                    failures.append(f"missing:resume_validation.{key}")
                elif normalized in PLACEHOLDERS:
                    failures.append(f"placeholder:resume_validation.{key}")
            continue
        normalized = _normalized_text(value)
        if not normalized:
            failures.append(f"missing:{field}")
        elif normalized in PLACEHOLDERS:
            failures.append(f"placeholder:{field}")

    state_session = str(state.get("session_id", ""))
    if state_session and session_id and state_session != session_id:
        failures.append("session:mismatch")

    next_action = _normalized_text(state.get("next_action", ""))
    if "checkpoint" in next_action and (
        "next action" in next_action or "next step" in next_action
    ):
        failures.append("circular:next_action")

    completed = {_normalized_text(item) for item in _as_list(state.get("completed_work"))}
    remaining = {_normalized_text(item) for item in _as_list(state.get("remaining_work"))}
    if completed & remaining:
        failures.append("overlap:completed_remaining")

    try:
        expected_transfer_id = derive_transfer_id(
            int(state.get("revision", 0)),
            str(state.get("transfer_nonce", "")),
        )
    except (TransferError, TypeError, ValueError):
        expected_transfer_id = ""
    if expected_transfer_id and state.get("transfer_id") != expected_transfer_id:
        failures.append("identity:transfer_id_mismatch")

    return {"resume_ready": not failures, "failures": failures}


def build_state(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "session_id": args.session_id.strip(),
        "revision": args.revision,
        "transfer_id": args.transfer_id,
        "goal_identity": args.goal_identity,
        "transfer_nonce": args.transfer_nonce,
        "objective": args.objective.strip(),
        "active_task": args.active_task.strip(),
        "phase": args.phase.strip(),
        "status": args.status.strip(),
        "completion_criteria": _as_list(args.completion_criteria),
        "completed_work": _as_list(args.completed_work),
        "remaining_work": _as_list(args.remaining_work),
        "constraints": _as_list(args.constraints),
        "decisions": _as_list(args.decisions),
        "blockers": _as_list(args.blockers),
        "authoritative_files": _as_list(args.authoritative_files),
        "validation_evidence": _as_list(args.validation_evidence),
        "resume_validation": {
            "command": args.resume_validation_command.strip(),
            "expected": args.resume_validation_expected.strip(),
        },
        "next_action": args.next_step.strip(),
    }


def _markdown_list(values: Sequence[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def render_kernel(
    state: dict[str, Any],
    *,
    resume_ready: bool,
    structural_failures: Sequence[str],
) -> str:
    sections = [
        "# Fresh Handoff Capsule v2",
        "",
        f"capsule_version: {CAPSULE_VERSION}",
        f"resume_ready: {'true' if resume_ready else 'false'}",
        f"session_id: {state['session_id']}",
        f"revision: {state['revision']}",
        f"transfer_id: {state['transfer_id']}",
        f"goal_identity: {state['goal_identity']}",
        f"transfer_nonce: {state['transfer_nonce']}",
    ]
    if structural_failures:
        sections.extend(["structural_failures:", _markdown_list(structural_failures)])
    opening = (
        ("Objective", [state["objective"]]),
        ("Phase / Status", [f"{state['phase']} / {state['status']}"]),
        ("Next Unfinished Action", [state["next_action"]]),
        ("Resume Validation Command", [state["resume_validation"]["command"]]),
        ("Resume Validation Expected", [state["resume_validation"]["expected"]]),
        ("Completion / Stop Condition", state["completion_criteria"]),
        ("Critical Constraints", state["constraints"]),
    )
    sections.extend(["", "## Opening Identity Kernel"])
    for title, values in opening:
        sections.extend(["", f"### {title}", "", _markdown_list(values)])

    middle = [
        ("Active Task", [state["active_task"]]),
        ("Remaining Work", state["remaining_work"]),
        ("Authoritative Files / Symbols", state["authoritative_files"]),
    ]
    absent_optional = []
    for field, title in OPTIONAL_SECTIONS:
        if state[field]:
            middle.append((title, state[field]))
        else:
            absent_optional.append(field)
    sections.extend(["", "## Supporting State"])
    if absent_optional:
        sections.extend(
            ["", "Absent optional state: " + ", ".join(absent_optional)]
        )
    for title, values in middle:
        sections.extend(["", f"### {title}", "", _markdown_list(values)])

    sections.extend(
        [
            "",
            "## Execution / Ownership Close",
            "",
            f"- Action now: {state['next_action']}",
            f"- Resume validation command: {state['resume_validation']['command']}",
            f"- Resume validation expected: {state['resume_validation']['expected']}",
            f"- Source session: {state['session_id']}",
            f"- Transfer ID: {state['transfer_id']}",
            f"- Goal identity: {state['goal_identity']}",
            f"- Revision: {state['revision']}",
            "- Capsule SHA-256: verify the exact transport SHA-256 against this file",
            f"- Nonce: {state['transfer_nonce']}",
            "- Acknowledge only after exact identity and resume-validation verification.",
            "- The destination becomes sole writer only after exact acknowledgement; before then the source remains authoritative.",
        ]
    )
    return "\n".join(sections).rstrip() + "\n"


def collect_optional_evidence(args: argparse.Namespace, repo: Path) -> list[tuple[str, str]]:
    evidence: list[tuple[str, str]] = []
    if args.goal_objective:
        evidence.append(("Goal Mode Objective", args.goal_objective.strip()))
    if args.reason:
        evidence.append(("Revision Reason", args.reason.strip()))
    if args.context_used:
        evidence.append(("Context Used", str(args.context_used)))
    if args.context_remaining:
        evidence.append(("Context Remaining", str(args.context_remaining)))
    if args.commands_run:
        evidence.append(("Commands Run", _markdown_list(args.commands_run)))
    if args.note:
        evidence.append(("Notes", _markdown_list(args.note)))

    status = trim_lines(git(repo, ["status", "--short"]), max(1, args.max_status_lines))
    if status != "(none)":
        evidence.append(("Git Status", f"```text\n{status.replace('```', '` ` `')}\n```"))
    diff_stat = trim_lines(git(repo, ["diff", "--stat"]), max(1, args.max_diff_lines))
    if diff_stat != "(none)":
        evidence.append(("Diff Stat", f"```text\n{diff_stat.replace('```', '` ` `')}\n```"))
    return evidence


def _render_optional(items: Sequence[tuple[str, str]]) -> str:
    if not items:
        return ""
    parts: list[str] = []
    for title, value in items:
        parts.extend(["", f"## {title}", "", value])
    return "\n".join(parts).rstrip() + "\n"


def write_content_addressed_overflow(
    root: Path,
    payload: dict[str, Any],
) -> dict[str, str]:
    content = (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(content).hexdigest()
    path = root / "overflow" / f"{digest}.json"
    if not path.exists():
        atomic_write_text(path, content.decode("utf-8"))
    return {"path": str(path), "sha256": digest}


def _overflow_reference(overflow: dict[str, str]) -> str:
    return (
        "\n## Overflow Evidence\n\n"
        f"path: {overflow['path']}\n"
        f"sha256: {overflow['sha256']}\n"
    )


def _safe_overflow_capsule(
    args: argparse.Namespace,
    overflow: dict[str, str],
    failure: str,
    budget: int,
) -> str:
    verbose = (
        "# Fresh Handoff Capsule v2\n\n"
        f"capsule_version: {CAPSULE_VERSION}\n"
        "resume_ready: false\n"
        f"session_id: {args.session_id}\n"
        f"revision: {args.revision}\n"
        f"structural_failures:\n- {failure}\n"
        f"overflow_path: {overflow['path']}\n"
        f"overflow_sha256: {overflow['sha256']}\n"
    )
    if len(verbose.encode("utf-8")) <= budget:
        return verbose

    relative_path = f"overflow/{Path(overflow['path']).name}"
    compact = (
        f"v:{CAPSULE_VERSION}\n"
        "resume_ready:false\n"
        f"rev:{args.revision}\n"
        "failure:overflow_ref_budget\n"
        f"overflow_path:{relative_path}\n"
        f"overflow_sha256:{overflow['sha256']}\n"
    )
    if len(compact.encode("utf-8")) <= budget:
        return compact
    raise ValueError("capsule byte budget is too small for safe overflow metadata")


def build_bounded_capsule(
    args: argparse.Namespace,
    repo: Path,
    state: dict[str, Any],
    out_path: Path,
) -> tuple[str, bool, dict[str, str] | None, dict[str, Any]]:
    guard = validate_structural_readiness(
        state,
        session_id=args.session_id,
    )
    kernel = render_kernel(
        state,
        resume_ready=bool(guard["resume_ready"]),
        structural_failures=guard["failures"],
    )
    budget = args.capsule_budget_bytes
    overflow: dict[str, str] | None
    if len(kernel.encode("utf-8")) > budget:
        overflow = write_content_addressed_overflow(
            session_state_dir(repo, args.session_id),
            {"kind": "critical", "state": state, "guard": guard},
        )
        safe = _safe_overflow_capsule(
            args,
            overflow,
            "critical:byte_budget_exceeded",
            budget,
        )
        return safe, False, overflow, {
            "resume_ready": False,
            "failures": [*guard["failures"], "critical:byte_budget_exceeded"],
        }

    optional = collect_optional_evidence(args, repo)
    included: list[tuple[str, str]] = []
    omitted: list[tuple[str, str]] = []
    for item in optional:
        candidate = kernel + _render_optional([*included, item])
        if len(candidate.encode("utf-8")) <= budget:
            included.append(item)
        else:
            omitted.append(item)

    overflow = None
    content = kernel + _render_optional(included)
    if omitted:
        overflow = write_content_addressed_overflow(
            session_state_dir(repo, args.session_id),
            {"kind": "optional", "evidence": omitted},
        )
        reference = _overflow_reference(overflow)
        while included and len((kernel + _render_optional(included) + reference).encode("utf-8")) > budget:
            omitted.insert(0, included.pop())
            overflow = write_content_addressed_overflow(
                session_state_dir(repo, args.session_id),
                {"kind": "optional", "evidence": omitted},
            )
            reference = _overflow_reference(overflow)
        candidate = kernel + _render_optional(included) + reference
        if len(candidate.encode("utf-8")) > budget:
            overflow = write_content_addressed_overflow(
                session_state_dir(repo, args.session_id),
                {
                    "kind": "critical_and_optional",
                    "state": state,
                    "guard": guard,
                    "evidence": omitted,
                },
            )
            failure = "critical:overflow_reference_budget_exceeded"
            safe = _safe_overflow_capsule(args, overflow, failure, budget)
            return safe, False, overflow, {
                "resume_ready": False,
                "failures": [*guard["failures"], failure],
            }
        content = candidate

    if len(content.encode("utf-8")) > budget:
        raise ValueError("unable to fit capsule within byte budget")
    return content, bool(guard["resume_ready"]), overflow, guard


def build_continuation_prompt(
    out_path: Path,
    prompt_budget_bytes: int = DEFAULT_PROMPT_BUDGET_BYTES,
    *,
    session_id: str = "",
    revision: int | None = None,
    capsule_sha256: str,
    goal_identity: str = "",
    transfer_nonce: str = "",
    transfer_id: str = "",
    next_action: str = "",
    resume_validation_command: str = "",
    resume_validation_expected: str = "",
) -> str | None:
    mandatory_lines = [
        f"Use $relay from {out_path}; read AGENTS.md + capsule; inspect repo/goal.",
        f"Expected session: {session_id or 'default'}. Expected revision: {revision if revision is not None else 'unspecified'}.",
        f"Expected capsule SHA-256: {capsule_sha256}. Expected goal identity: {goal_identity}.",
        f"Expected transfer nonce: {transfer_nonce}. Expected transfer ID: {transfer_id}.",
        "Verify SHA-256 + identity.",
        f"Exact next action: {next_action}.",
        f"Resume validation command: {resume_validation_command}.",
        f"Resume validation expected: {resume_validation_expected}.",
        "transfer_control.py: verify, then acknowledge.",
        "Source authoritative/destination control-only until exact acknowledgement; destination sole writer after. Wait for status can_continue:true.",
    ]
    mandatory = "\n".join(mandatory_lines)
    mandatory_bytes = len(mandatory.encode("utf-8"))
    if mandatory_bytes > prompt_budget_bytes:
        return None
    return mandatory


def _base_metrics(
    args: argparse.Namespace,
    *,
    capsule_bytes: int = 0,
    prompt_bytes: int = 0,
) -> dict[str, Any]:
    total = capsule_bytes + prompt_bytes
    return {
        "capsule_budget_bytes": args.capsule_budget_bytes,
        "capsule_bytes": capsule_bytes,
        "prompt_budget_bytes": args.prompt_budget_bytes,
        "prompt_bytes": prompt_bytes,
        "approximate_tokens": (total + 3) // 4,
        "token_estimate_label": "approximate UTF-8 byte proxy; not a pass/fail gate",
        "handoff_trigger_ratio": args.handoff_threshold,
    }


def emit_json_or_path(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if args.emit_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    elif payload.get("capsule_path"):
        print(payload["capsule_path"])


def _main() -> int:
    args = parse_args()
    repo = resolve_repo(args.repo)
    if not repo.exists():
        print(f"Repository path does not exist: {repo}", file=sys.stderr)
        return 2
    if not 0 <= args.handoff_threshold <= 1:
        print("handoff threshold must be a ratio between 0 and 1", file=sys.stderr)
        return 2
    budget_error = byte_budget_limit_error(
        args.capsule_budget_bytes,
        args.prompt_budget_bytes,
    )
    if budget_error:
        print(budget_error, file=sys.stderr)
        return 2
    if args.capsule_budget_bytes < 256 or args.prompt_budget_bytes < 128:
        print("byte budgets are below safe metadata minimums", file=sys.stderr)
        return 2

    actor_session_id = args.session_id.strip() or "default"
    source_session_id = args.source_session_id.strip() or actor_session_id
    args.goal_identity = derive_goal_identity(
        args.goal_identity,
        args.goal_objective,
        args.objective,
    )
    args.transfer_nonce = args.transfer_nonce.strip() or secrets.token_urlsafe(24)
    try:
        args.transfer_id = derive_transfer_id(args.revision, args.transfer_nonce)
    except TransferError as exc:
        print(f"invalid transfer identity: {exc}", file=sys.stderr)
        return 2

    context_ratio = parse_ratio(args.context_used)
    threshold_blocked = (
        not args.force_handoff
        and args.context_used is not None
        and context_ratio is not None
        and context_ratio < args.handoff_threshold
    )
    if threshold_blocked:
        payload = {
            "should_handoff": False,
            "checkpoint_written": False,
            "capsule_path": None,
            "continuation_prompt": None,
            "context_used_ratio": context_ratio,
            "handoff_threshold": args.handoff_threshold,
            "handoff_trigger_ratio": args.handoff_threshold,
            "resume_ready": False,
            "delivery_emitted": False,
            "skipped": True,
            "skip_reason": "context-used below threshold",
            "metrics": _base_metrics(args),
        }
        emit_json_or_path(args, payload)
        return 0

    authority = ExitStack()
    _OPEN_AUTHORITIES.append(authority)
    try:
        authority.enter_context(
            authority_transaction(
                repo,
                actor_session_id=actor_session_id,
                source_session_id=source_session_id,
            )
        )
    except TransferError as exc:
        print(f"write authority denied: {exc}", file=sys.stderr)
        return 3

    state = build_state(args)
    if args.update_active_task_only:
        task_path = save_active_task(
            repo,
            session_id=args.session_id,
            objective=state["objective"],
            next_action=state["next_action"],
            goal_objective=args.goal_objective,
            reason=args.reason,
            active_task=state["active_task"],
            phase=state["phase"],
            status=state["status"],
            completion_criteria=state["completion_criteria"],
            completed_work=state["completed_work"],
            remaining_work=state["remaining_work"],
            constraints=state["constraints"],
            decisions=state["decisions"],
            blockers=state["blockers"],
            authoritative_files=state["authoritative_files"],
            validation_evidence=state["validation_evidence"],
            resume_validation_command=state["resume_validation"]["command"],
            resume_validation_expected=state["resume_validation"]["expected"],
            revision=args.revision,
            goal_identity=args.goal_identity,
            transfer_nonce=args.transfer_nonce,
            transfer_id=args.transfer_id,
        )
        payload = {
            "active_task_path": str(task_path),
            "session_id": args.session_id,
            "updated": True,
        }
        _close_authority(authority)
        emit_json_or_path(args, payload)
        return 0

    now = dt.datetime.now().astimezone()
    out_path = (
        Path(args.out).expanduser().resolve()
        if args.out
        else default_out_path(repo, now, args.session_id, args.revision)
    )
    try:
        content, resume_ready, overflow, guard = build_bounded_capsule(
            args,
            repo,
            state,
            out_path,
        )
    except ValueError as exc:
        _close_authority(authority)
        print(str(exc), file=sys.stderr)
        return 2
    atomic_write_text(out_path, content)
    capsule_bytes = len(content.encode("utf-8"))
    capsule_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()

    prompt = None
    prompt_guard: dict[str, Any] = {
        "fits": None,
        "budget_bytes": args.prompt_budget_bytes,
        "reason": "capsule is not structurally ready",
    }
    if resume_ready:
        prompt = build_continuation_prompt(
            out_path,
            args.prompt_budget_bytes,
            session_id=args.session_id,
            revision=args.revision,
            capsule_sha256=capsule_sha256,
            goal_identity=args.goal_identity,
            transfer_nonce=args.transfer_nonce,
            transfer_id=args.transfer_id,
            next_action=state["next_action"],
            resume_validation_command=state["resume_validation"]["command"],
            resume_validation_expected=state["resume_validation"]["expected"],
        )
        prompt_guard = {
            "fits": prompt is not None,
            "budget_bytes": args.prompt_budget_bytes,
            "reason": (
                None
                if prompt is not None
                else "mandatory continuation prompt exceeds prompt byte budget"
            ),
        }
    prompt_bytes = len(prompt.encode("utf-8")) if prompt else 0
    should_handoff = bool(
        resume_ready
        and prompt is not None
        and (
            args.force_handoff
            or args.context_used is None
            or (
                context_ratio is not None
                and context_ratio >= args.handoff_threshold
            )
        )
    )

    if should_persist_active_task(repo, out_path, args.update_active_task_only, args.session_id):
        save_active_task(
            repo,
            session_id=args.session_id,
            objective=state["objective"],
            next_action=state["next_action"],
            goal_objective=args.goal_objective,
            capsule_path=str(out_path),
            reason=args.reason,
            active_task=state["active_task"],
            phase=state["phase"],
            status=state["status"],
            completion_criteria=state["completion_criteria"],
            completed_work=state["completed_work"],
            remaining_work=state["remaining_work"],
            constraints=state["constraints"],
            decisions=state["decisions"],
            blockers=state["blockers"],
            authoritative_files=state["authoritative_files"],
            validation_evidence=state["validation_evidence"],
            resume_validation_command=state["resume_validation"]["command"],
            resume_validation_expected=state["resume_validation"]["expected"],
            revision=args.revision,
            goal_identity=args.goal_identity,
            transfer_nonce=args.transfer_nonce,
            transfer_id=args.transfer_id,
        )

    metrics = _base_metrics(
        args,
        capsule_bytes=capsule_bytes,
        prompt_bytes=prompt_bytes,
    )
    metrics.update(
        {
            "revision_reason": args.reason,
            "structural_guard": guard,
            "resume_ready": resume_ready,
            "overflow": overflow,
            "delivery_emitted": should_handoff,
            "deduped": False,
            "paired_run_id": None,
            "prompt_guard": prompt_guard,
        }
    )
    prompt_skip_reason = (
        "mandatory continuation prompt exceeds prompt byte budget"
        if resume_ready and prompt is None
        else None
    )
    payload = {
        "contract_version": CAPSULE_VERSION,
        "should_handoff": should_handoff,
        "checkpoint_written": True,
        "revision_created": True,
        "capsule_path": str(out_path),
        "capsule_sha256": capsule_sha256,
        "continuation_prompt": prompt,
        "context_used_ratio": context_ratio,
        "handoff_threshold": args.handoff_threshold,
        "handoff_trigger_ratio": args.handoff_threshold,
        "session_id": args.session_id,
        "session_scope": session_scope(args.session_id),
        "revision": args.revision,
        "goal_identity": args.goal_identity,
        "transfer_nonce": args.transfer_nonce,
        "transfer_id": args.transfer_id,
        "next_action": state["next_action"],
        "validation_evidence": state["validation_evidence"],
        "resume_validation": state["resume_validation"],
        "resume_ready": resume_ready,
        "structural_guard": guard,
        "prompt_guard": prompt_guard,
        "overflow": overflow,
        "delivery_emitted": should_handoff,
        "delivered": should_handoff,
        "skipped": prompt_skip_reason is not None,
        "skip_reason": prompt_skip_reason,
        "metrics": metrics,
    }
    _close_authority(authority)
    emit_json_or_path(args, payload)
    return 0


def main() -> int:
    try:
        return _main()
    finally:
        for authority in list(_OPEN_AUTHORITIES):
            _close_authority(authority)


if __name__ == "__main__":
    raise SystemExit(main())
