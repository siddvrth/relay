#!/usr/bin/env python3
"""Orchestrate revision creation and continuation delivery for handoffs."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

try:
    fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - Unix is the primary runtime.
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
from write_handoff import (  # noqa: E402
    DEFAULT_CAPSULE_BUDGET_BYTES,
    DEFAULT_HANDOFF_TRIGGER_RATIO,
    DEFAULT_PROMPT_BUDGET_BYTES,
    atomic_write_json,
    byte_budget_limit_error,
    derive_goal_identity,
    load_active_task,
    session_scope,
    state_root,
    validate_structural_readiness,
)
from transfer_control import (  # noqa: E402
    TransferError,
    authority_transaction,
    discover_source_for_actor as discover_transfer_source,
    guard_write,
    hook_pretool_decision,
    prepare as prepare_transfer,
)


DEFAULT_THRESHOLD = DEFAULT_HANDOFF_TRIGGER_RATIO
DEFAULT_DEDUP_SECONDS = 300
HANDOFF_MODE = "clean_task"
WRITE_HANDOFF = Path(__file__).with_name("write_handoff.py")

PERCENT_FIELD_NAMES = (
    "context_usage_percent",
    "contextUsagePercent",
    "context_used_percent",
    "contextUsedPercent",
    "tokenUsagePercent",
    "token_usage_percent",
    "usagePercent",
    "usage_percent",
)
RATIO_FIELD_NAMES = (
    "contextUsed",
    "context_used",
    "contextUsage",
    "context_usage",
    "contextUsedRatio",
    "context_used_ratio",
    "usageRatio",
    "usage_ratio",
)
SESSION_FIELD_NAMES = (
    "session_id",
    "sessionId",
    "conversation_id",
    "conversationId",
    "composer_id",
    "composerId",
    "thread_id",
    "threadId",
)


@dataclass(frozen=True)
class StatePaths:
    root: Path
    session_dir: Path
    active_task: Path
    revision: Path
    delivery: Path
    lock: Path
    session_pointer: Path
    latest_pointer: Path
    prepare_intent: Path


def state_paths(repo: Path, session_id: str | None = None) -> StatePaths:
    root = state_root(repo)
    session_dir = root / "sessions" / session_scope(session_id)
    return StatePaths(
        root=root,
        session_dir=session_dir,
        active_task=session_dir / ".active-task.json",
        revision=session_dir / ".revision.json",
        delivery=session_dir / ".delivery.json",
        lock=session_dir / ".handoff.lock",
        session_pointer=session_dir / ".pointer.json",
        latest_pointer=root / ".latest.json",
        prepare_intent=session_dir / ".prepare-intent.json",
    )


def _append_argument(
    parser: argparse.ArgumentParser,
    *flags: str,
    dest: str,
) -> None:
    parser.add_argument(*flags, dest=dest, action="append", default=[])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Checkpoint-and-continue internal orchestrator."
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--stdin-json", action="store_true")
    parser.add_argument("--context-used")
    parser.add_argument("--handoff-threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--source-session-id", default="")
    parser.add_argument("--goal-identity", default="")
    parser.add_argument("--handoff-key", default="")
    parser.add_argument(
        "--objective",
        default=os.environ.get("CHECKPOINT_AND_CONTINUE_OBJECTIVE", ""),
    )
    parser.add_argument("--active-task", default="")
    parser.add_argument("--phase", default="")
    parser.add_argument("--status", default="")
    _append_argument(parser, "--completion-criteria", dest="completion_criteria")
    _append_argument(parser, "--completed-work", "--completed", dest="completed_work")
    _append_argument(parser, "--remaining-work", "--remaining", dest="remaining_work")
    _append_argument(parser, "--constraints", "--constraint", dest="constraints")
    parser.add_argument(
        "--next-step",
        "--next-action",
        dest="next_step",
        default=os.environ.get("CHECKPOINT_AND_CONTINUE_NEXT_STEP", ""),
    )
    parser.add_argument(
        "--goal-objective",
        default=os.environ.get("CHECKPOINT_AND_CONTINUE_GOAL_OBJECTIVE", ""),
    )
    parser.add_argument("--reason", default="context threshold handoff")
    parser.add_argument(
        "--trigger",
        choices=("threshold", "pre-compact", "manual", "stop"),
        default="threshold",
    )
    parser.add_argument("--force-handoff", action="store_true")
    parser.add_argument("--dedup-seconds", type=int, default=DEFAULT_DEDUP_SECONDS)
    _append_argument(parser, "--decisions", "--decision", dest="decisions")
    _append_argument(parser, "--blockers", "--blocker", dest="blockers")
    _append_argument(
        parser,
        "--validation-evidence",
        "--validation-status",
        dest="validation_evidence",
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
    )
    _append_argument(parser, "--commands-run", "--command-run", dest="commands_run")
    _append_argument(parser, "--note", dest="note")
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
    parser.add_argument(
        "--official-hook-event",
        choices=("UserPromptSubmit", "PreToolUse", "PreCompact", "Stop"),
    )
    return parser.parse_args()


def resolve_repo(path: str) -> Path:
    candidate = Path(path).expanduser().resolve()
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(candidate),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return Path(completed.stdout.strip()).resolve()
    return candidate


def _finite_ratio(value: float) -> float | None:
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return value


def parse_ratio(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
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
    return _finite_ratio(parsed)


def parse_percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    try:
        text = str(value).strip().lower()
        if text.endswith("%"):
            text = text[:-1].strip()
        parsed = float(text) / 100
    except (TypeError, ValueError, OverflowError):
        return None
    return _finite_ratio(parsed)


def extract_context_used(payload: dict[str, Any]) -> float | None:
    for key in PERCENT_FIELD_NAMES:
        if key in payload:
            ratio = parse_percent(payload[key])
            if ratio is not None:
                return ratio

    for key in RATIO_FIELD_NAMES:
        if key in payload:
            ratio = parse_ratio(payload[key])
            if ratio is not None:
                return ratio

    tokens = payload.get("context_tokens")
    window = payload.get("context_window_size")
    if tokens is not None and window is not None:
        try:
            tokens_f = float(tokens)
            window_f = float(window)
            if math.isfinite(tokens_f) and math.isfinite(window_f) and tokens_f >= 0 and window_f > 0:
                ratio = _finite_ratio(tokens_f / window_f)
                if ratio is not None:
                    return ratio
        except (TypeError, ValueError, OverflowError):
            pass

    for container_key in ("context", "telemetry", "usage", "metrics"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            ratio = extract_context_used(container)
            if ratio is not None:
                return ratio
    return None


def extract_handoff_key(payload: dict[str, Any]) -> str | None:
    for key in SESSION_FIELD_NAMES:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    for container_key in ("context", "telemetry", "usage", "metrics", "session"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            value = extract_handoff_key(container)
            if value:
                return value
    return None


def extract_source_session(payload: dict[str, Any]) -> str | None:
    for key in ("source_session_id", "sourceSessionId", "handoff_source_session_id"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def discover_source_for_actor(repo: Path, actor_session_id: str) -> str | None:
    return discover_transfer_source(repo, actor_session_id)


def handoff_scope(value: str | None) -> str | None:
    return session_scope(value) if value else None


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def recently_handed_off(path: Path, dedup_seconds: int) -> bool:
    record = read_json(path)
    if not record:
        return False
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, (int, float)) or not math.isfinite(float(timestamp)):
        return False
    return (time.time() - float(timestamp)) < max(0, dedup_seconds)


@contextmanager
def handoff_lock(path: Path):
    if fcntl is None:
        raise RuntimeError("session handoff locking is unavailable")
    # The lock file is created in an authority transaction before this context.
    # Nested mutation paths acquire this session lock and then transfer authority;
    # no path holds transfer authority while acquiring this session lock.
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _list_value(explicit: Sequence[str], active: dict[str, Any], key: str) -> list[str]:
    if explicit:
        return [str(item).strip() for item in explicit if str(item).strip()]
    value = active.get(key, [])
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if value is not None and str(value).strip() else []


def _text_value(explicit: str, active: dict[str, Any], *keys: str) -> str:
    if explicit.strip():
        return explicit.strip()
    for key in keys:
        value = active.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _resume_validation_value(
    explicit: str,
    active: dict[str, Any],
    key: str,
) -> str:
    if explicit.strip():
        return explicit.strip()
    value = active.get("resume_validation")
    if isinstance(value, dict):
        selected = value.get(key)
        if selected is not None and str(selected).strip():
            return str(selected).strip()
    return ""


def merge_state(args: argparse.Namespace, active: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": args.session_id,
        "objective": _text_value(args.objective, active, "objective"),
        "active_task": _text_value(args.active_task, active, "active_task"),
        "phase": _text_value(args.phase, active, "phase"),
        "status": _text_value(args.status, active, "status"),
        "completion_criteria": _list_value(
            args.completion_criteria, active, "completion_criteria"
        ),
        "completed_work": _list_value(args.completed_work, active, "completed_work"),
        "remaining_work": _list_value(args.remaining_work, active, "remaining_work"),
        "constraints": _list_value(args.constraints, active, "constraints"),
        "decisions": _list_value(args.decisions, active, "decisions"),
        "blockers": _list_value(args.blockers, active, "blockers"),
        "authoritative_files": _list_value(
            args.authoritative_files, active, "authoritative_files"
        ),
        "validation_evidence": (
            _list_value(args.validation_evidence, active, "validation_evidence")
            if args.validation_evidence
            else _list_value([], active, "validation_evidence")
            or _list_value([], active, "validation")
        ),
        "resume_validation": {
            "command": _resume_validation_value(
                args.resume_validation_command, active, "command"
            ),
            "expected": _resume_validation_value(
                args.resume_validation_expected, active, "expected"
            ),
        },
        "next_action": _text_value(args.next_step, active, "next_action", "next_step"),
        "goal_objective": _text_value(args.goal_objective, active, "goal_objective"),
        "goal_identity": args.goal_identity,
    }


def canonical_state_hash(state: dict[str, Any]) -> str:
    encoded = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def validate_reusable_pointer(
    pointer: dict[str, Any],
    *,
    paths: StatePaths,
    session_id: str,
    revision: int,
) -> list[str]:
    failures: list[str] = []
    if pointer.get("session_id") != session_id:
        failures.append("pointer:session_mismatch")
    if pointer.get("session_scope") != session_scope(session_id):
        failures.append("pointer:scope_mismatch")
    if pointer.get("revision") != revision:
        failures.append("pointer:revision_mismatch")
    if pointer.get("resume_ready") is not True:
        failures.append("pointer:not_resume_ready")
    for key in ("goal_identity", "transfer_nonce", "transfer_id"):
        if not isinstance(pointer.get(key), str) or not pointer.get(key):
            failures.append(f"pointer:missing_{key}")

    raw_path = pointer.get("capsule_path")
    capsule_path: Path | None = None
    if not isinstance(raw_path, str) or not raw_path:
        failures.append("pointer:invalid_path")
    else:
        try:
            capsule_path = Path(raw_path).expanduser().resolve(strict=True)
            capsule_path.relative_to(paths.session_dir.resolve())
        except (OSError, RuntimeError, ValueError):
            failures.append("pointer:path_not_contained")
            capsule_path = None
        else:
            if not capsule_path.is_file():
                failures.append("pointer:path_not_file")
                capsule_path = None

    expected_sha256 = pointer.get("capsule_sha256")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        failures.append("pointer:invalid_sha256")
    elif capsule_path is not None:
        try:
            actual_sha256 = hashlib.sha256(capsule_path.read_bytes()).hexdigest()
        except OSError:
            failures.append("pointer:unreadable_capsule")
        else:
            if actual_sha256 != expected_sha256:
                failures.append("pointer:sha256_mismatch")
    return failures


def invoke_write_handoff(
    repo: Path,
    args: argparse.Namespace,
    state: dict[str, Any],
    *,
    revision: int,
    context_used: str | None,
    out_path: Path,
    source_session_id: str,
    transfer_nonce: str,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(WRITE_HANDOFF),
        "--repo",
        str(repo),
        "--out",
        str(out_path),
        "--session-id",
        args.session_id,
        "--source-session-id",
        source_session_id,
        "--goal-identity",
        state["goal_identity"],
        f"--transfer-nonce={transfer_nonce}",
        "--revision",
        str(revision),
        "--objective",
        state["objective"],
        "--active-task",
        state["active_task"],
        "--phase",
        state["phase"],
        "--status",
        state["status"],
        "--next-step",
        state["next_action"],
        "--reason",
        args.reason,
        "--handoff-threshold",
        str(args.handoff_threshold),
        "--capsule-budget-bytes",
        str(args.capsule_budget_bytes),
        "--prompt-budget-bytes",
        str(args.prompt_budget_bytes),
        "--force-handoff",
        "--emit-json",
    ]
    if context_used is not None:
        command.extend(["--context-used", context_used])
    if state["goal_objective"]:
        command.extend(["--goal-objective", state["goal_objective"]])
    for flag, key in (
        ("--completion-criteria", "completion_criteria"),
        ("--completed-work", "completed_work"),
        ("--remaining-work", "remaining_work"),
        ("--constraints", "constraints"),
        ("--decisions", "decisions"),
        ("--blockers", "blockers"),
        ("--authoritative-files", "authoritative_files"),
    ):
        for value in state[key]:
            command.extend([flag, value])
    for value in state["validation_evidence"]:
        command.extend(["--validation-evidence", value])
    command.extend(
        [
            "--resume-validation-command",
            state["resume_validation"]["command"],
            "--resume-validation-expected",
            state["resume_validation"]["expected"],
        ]
    )
    for value in args.commands_run:
        command.extend(["--commands-run", value])
    for value in args.note:
        command.extend(["--note", value])

    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or completed.stdout.strip() or "write_handoff failed"
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"write_handoff returned invalid JSON: {completed.stdout}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("write_handoff returned a non-object JSON value")
    return value


def should_trigger_handoff(
    *,
    trigger: str,
    context_ratio: float | None,
    threshold: float,
    force: bool,
) -> bool:
    if force or trigger in {"pre-compact", "manual"}:
        return True
    if context_ratio is None:
        return False
    return context_ratio >= threshold


def build_user_message(context_ratio: float | None, threshold: float, trigger: str) -> str:
    if trigger == "pre-compact":
        return "A fresh checkpoint revision was written before compaction."
    if context_ratio is not None:
        return f"Context usage reached {context_ratio:.0%} (threshold {threshold:.0%})."
    return "A fresh checkpoint revision was written."


def lifecycle_next_actions(
    repo: Path,
    transfer: dict[str, Any],
    *,
    create_thread_available: bool,
) -> list[dict[str, Any]]:
    source = str(transfer.get("session_id") or transfer.get("source_session_id") or "<source-session-id>")
    transfer_id = str(transfer.get("transfer_id") or "<transfer-id>")
    script = str(Path(__file__).with_name("transfer_control.py").resolve())
    base = [sys.executable, script, "--repo", str(repo.resolve())]
    exact = [
        "--source-session-id", source,
        "--transfer-id", transfer_id,
        "--destination-session-id", "<destination-session-id>",
        "--destination-task-id", "<destination-task-id>",
        "--goal-identity", str(transfer.get("goal_identity") or "<goal-identity>"),
        "--capsule-path", str(transfer.get("capsule_path") or "<capsule-path>"),
        "--capsule-revision", str(transfer.get("revision") or "<capsule-revision>"),
        "--capsule-sha256", str(transfer.get("capsule_sha256") or "<capsule-sha256>"),
        "--nonce", str(transfer.get("transfer_nonce") or "<nonce>"),
    ]
    return [
        {
            "phase": "launch_requested",
            "command_argv": [*base, "launch-requested", "--source-session-id", source, "--transfer-id", transfer_id, "--transport-key", "<create-intent-id>"],
        },
        {
            "phase": "create_clean_task",
            "app_action": "create_thread",
            "available": create_thread_available,
            "rule": "record launch_requested before invoking; unknown outcome must be reconciled, never blindly retried",
        },
        {
            "phase": "delivered_and_started",
            "commands_argv": [
                [*base, "delivered", "--source-session-id", source, "--transfer-id", transfer_id, "--transport-key", "<create-intent-id>", "--destination-task-id", "<destination-task-id>"],
                [*base, "started", "--source-session-id", source, "--transfer-id", transfer_id, "--destination-session-id", "<destination-session-id>", "--destination-task-id", "<destination-task-id>"],
            ],
        },
        {
            "phase": "verify_and_acknowledge",
            "rule": "destination must supply every exact capsule identity field from the active transfer before acknowledgement",
            "commands_argv": [
                [
                    *base,
                    "verify",
                    *exact,
                    "--repository-inspected",
                    "--goal-inspected",
                    "--exact-next-action",
                    str(transfer.get("next_action") or "<exact-next-action>"),
                    "--resume-validation-command",
                    str(
                        (transfer.get("resume_validation") or {}).get("command")
                        or "<resume-validation-command>"
                    ),
                    "--resume-validation-expected",
                    str(
                        (transfer.get("resume_validation") or {}).get("expected")
                        or "<resume-validation-expected>"
                    ),
                ],
                [*base, "acknowledge", *exact],
            ],
        },
        {
            "phase": "source_stop",
            "commands_argv": [
                [*base, "request-stop", "--source-session-id", source, "--transfer-id", transfer_id, "--capability", "<verified-capability>"],
                [*base, "record-stop", "--source-session-id", source, "--transfer-id", transfer_id, "--result", "termination_pending"],
            ],
            "host_adapter_success_command_argv": [
                *base,
                "record-stop",
                "--source-session-id", source,
                "--transfer-id", transfer_id,
                "--result", "<compatible-final-result>",
                "--evidence-kind", "<capability-result-evidence-kind>",
                "--evidence-reference", "<durable-adapter-evidence-reference>",
            ],
            "rule": "prompt-driven paths may record only unsupported, failed, or termination_pending; success requires host-adapter evidence",
        },
    ]


def app_capability_guidance(
    payload: dict[str, Any],
    *,
    repo: Path | None = None,
    transfer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = payload.get("available_thread_tools") or payload.get("availableTools") or []
    available = {str(item) for item in raw} if isinstance(raw, list) else set()
    return {
        "capability_gated": True,
        "create_clean_task_supported": "create_thread" in available,
        "observe_destination_supported": "read_thread" in available,
        "target_interrupt_isolation_supported": False,
        "handoff_thread_candidate_available": "handoff_thread" in available,
        "handoff_thread_rule": (
            "candidate only after acknowledgement, verified target authority, and checkout compatibility; never generic interrupt or close support"
            if "handoff_thread" in available
            else "unavailable; persist termination_pending after enforced read-only"
        ),
        "visible_archive_is_separate": True,
        "lifecycle_next_actions": (
            lifecycle_next_actions(
                repo,
                transfer,
                create_thread_available="create_thread" in available,
            )
            if repo is not None and transfer is not None
            else []
        ),
    }


def _empty_result(args: argparse.Namespace, context_ratio: float | None) -> dict[str, Any]:
    return {
        "contract_version": 2,
        "should_handoff": False,
        "checkpoint_written": False,
        "revision_created": False,
        "capsule_path": None,
        "capsule_sha256": None,
        "continuation_prompt": None,
        "user_message": None,
        "context_used_ratio": context_ratio,
        "handoff_threshold": args.handoff_threshold,
        "handoff_trigger_ratio": args.handoff_threshold,
        "deduped": False,
        "handoff_scope": handoff_scope(args.session_id),
        "session_scope": session_scope(args.session_id),
        "session_id": args.session_id,
        "handoff_mode": HANDOFF_MODE,
        "revision": None,
        "resume_ready": False,
        "delivery_emitted": False,
        "delivered": False,
        "skip_reason": None,
        "metrics": {
            "capsule_budget_bytes": args.capsule_budget_bytes,
            "capsule_bytes": 0,
            "prompt_budget_bytes": args.prompt_budget_bytes,
            "prompt_bytes": 0,
            "handoff_trigger_ratio": args.handoff_threshold,
            "token_estimate_label": "approximate UTF-8 byte proxy; not a pass/fail gate",
        },
    }


def official_hook_response(
    event: str,
    internal: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    guard = internal.get("write_guard")
    denied = isinstance(guard, dict) and not guard.get("allowed", False)
    reason = str(guard.get("reason", "write authority denied")) if denied else ""
    if event == "PreToolUse":
        repo_value = internal.get("_guard_repo")
        if isinstance(repo_value, str) and repo_value:
            return hook_pretool_decision(Path(repo_value), payload or {})
        if not denied:
            return {"continue": True}
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }

    if denied:
        if event == "UserPromptSubmit":
            if reason == "destination_embargoed":
                return {"continue": True}
            return {"continue": True, "decision": "block", "reason": reason}
        if event in {"PreCompact", "Stop"}:
            return {"continue": False, "stopReason": reason}

    if event == "UserPromptSubmit":
        prompt = internal.get("continuation_prompt")
        if internal.get("delivery_emitted") and isinstance(prompt, str) and prompt:
            return {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": prompt,
                },
            }
        return {"continue": True}

    if event == "PreCompact":
        if internal.get("checkpoint_written"):
            return {
                "continue": True,
                "systemMessage": "Checkpoint-and-continue state refreshed before compaction.",
            }
        return {"continue": True}

    if event == "Stop":
        if internal.get("checkpoint_written"):
            return {
                "continue": True,
                "systemMessage": "Checkpoint-and-continue state preserved.",
            }
        return {"continue": True}

    return {"continue": True}


def _authorized_json_writes(
    repo: Path,
    *,
    actor_session_id: str,
    source_session_id: str,
    writes: Sequence[tuple[Path, dict[str, Any]]],
) -> None:
    with authority_transaction(
        repo,
        actor_session_id=actor_session_id,
        source_session_id=source_session_id,
    ):
        for path, payload in writes:
            atomic_write_json(path, payload)


def _authorized_unlink(
    repo: Path,
    *,
    actor_session_id: str,
    source_session_id: str,
    path: Path,
) -> None:
    with authority_transaction(
        repo,
        actor_session_id=actor_session_id,
        source_session_id=source_session_id,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def invoke(repo: Path, args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    budget_error = byte_budget_limit_error(
        args.capsule_budget_bytes,
        args.prompt_budget_bytes,
    )
    if budget_error:
        raise ValueError(budget_error)

    context_ratio = parse_ratio(args.context_used)
    if context_ratio is None:
        context_ratio = extract_context_used(payload)

    payload_session = extract_handoff_key(payload) or ""
    explicit_session = args.session_id.strip() or args.handoff_key.strip()
    args.session_id = explicit_session or payload_session
    actor_session_id = args.session_id.strip() or "default"
    source_session_id = (
        args.source_session_id.strip()
        or extract_source_session(payload)
        or discover_source_for_actor(repo, actor_session_id)
        or actor_session_id
    )
    args.source_session_id = source_session_id
    args.goal_identity = derive_goal_identity(
        args.goal_identity,
        args.goal_objective,
        args.objective,
    )

    result = _empty_result(args, context_ratio)
    guard = guard_write(
        repo,
        actor_session_id=actor_session_id,
        source_session_id=source_session_id,
    )
    result["write_guard"] = guard
    if not guard["allowed"]:
        result["skip_reason"] = str(guard["reason"])
        result["source_revoked"] = guard["reason"] in {
            "actor_revoked",
            "source_revoked",
        }
        return result
    if not should_trigger_handoff(
        trigger=args.trigger,
        context_ratio=context_ratio,
        threshold=args.handoff_threshold,
        force=args.force_handoff,
    ):
        return result

    paths = state_paths(repo, args.session_id)
    with authority_transaction(
        repo,
        actor_session_id=actor_session_id,
        source_session_id=source_session_id,
    ):
        paths.session_dir.mkdir(parents=True, exist_ok=True)
        paths.lock.touch(exist_ok=True)
    with handoff_lock(paths.lock):
        active = load_active_task(repo, args.session_id)
        state = merge_state(args, active)
        state_hash = canonical_state_hash(state)
        previous_revision = read_json(paths.revision) or {}
        previous_pointer = read_json(paths.session_pointer) or {}
        last_revision = previous_revision.get("revision", 0)
        if type(last_revision) is not int or last_revision < 0:
            last_revision = 0

        delivery_recent = recently_handed_off(paths.delivery, args.dedup_seconds)
        same_state = previous_revision.get("state_sha256") == state_hash
        pointer_failures = (
            validate_reusable_pointer(
                previous_pointer,
                paths=paths,
                session_id=args.session_id,
                revision=last_revision,
            )
            if previous_pointer
            else ["pointer:missing"]
        )
        prepare_intent = read_json(paths.prepare_intent)
        if prepare_intent:
            intent_revision = prepare_intent.get("revision")
            intent_path_raw = prepare_intent.get("capsule_path")
            try:
                intent_path = Path(str(intent_path_raw)).expanduser().resolve()
                intent_path.relative_to(paths.session_dir.resolve())
            except (OSError, RuntimeError, ValueError):
                raise RuntimeError("prepare intent capsule path is invalid")
            if (
                prepare_intent.get("session_id") != args.session_id
                or prepare_intent.get("source_session_id") != source_session_id
                or prepare_intent.get("state_sha256") != state_hash
                or prepare_intent.get("goal_identity") != state["goal_identity"]
                or type(intent_revision) is not int
                or intent_revision < 1
                or intent_revision not in {last_revision, last_revision + 1}
                or not isinstance(prepare_intent.get("transfer_nonce"), str)
                or not prepare_intent["transfer_nonce"]
            ):
                raise RuntimeError("prepare intent conflicts with current handoff state")
        create_revision = bool(
            prepare_intent
            or
            args.trigger == "pre-compact"
            or not same_state
            or not delivery_recent
            or pointer_failures
        )

        if create_revision:
            if prepare_intent:
                revision = int(prepare_intent["revision"])
                transfer_nonce = str(prepare_intent["transfer_nonce"])
                output_path = Path(str(prepare_intent["capsule_path"])).resolve()
            else:
                revision = last_revision + 1
                transfer_nonce = secrets.token_urlsafe(24)
                output_path = paths.session_dir / (
                    f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}"
                    f"-r{revision}-handoff.md"
                )
                prepare_intent = {
                    "version": 1,
                    "session_id": args.session_id,
                    "source_session_id": source_session_id,
                    "state_sha256": state_hash,
                    "goal_identity": state["goal_identity"],
                    "revision": revision,
                    "capsule_path": str(output_path),
                    "transfer_nonce": transfer_nonce,
                    "writer_complete": False,
                }
                _authorized_json_writes(
                    repo,
                    actor_session_id=actor_session_id,
                    source_session_id=source_session_id,
                    writes=((paths.prepare_intent, prepare_intent),),
                )
            retained_result = prepare_intent.get("write_result")
            write_result = dict(retained_result) if isinstance(retained_result, dict) else {}
            retained_sha = write_result.get("capsule_sha256")
            reusable_write = bool(
                prepare_intent.get("writer_complete") is True
                and isinstance(retained_sha, str)
                and output_path.is_file()
                and hashlib.sha256(output_path.read_bytes()).hexdigest() == retained_sha
                and write_result.get("capsule_path") == str(output_path)
                and write_result.get("transfer_nonce") == transfer_nonce
            )
            if not reusable_write:
                write_result = invoke_write_handoff(
                    repo,
                    args,
                    state,
                    revision=revision,
                    context_used=f"{context_ratio:.6f}" if context_ratio is not None else None,
                    out_path=output_path,
                    source_session_id=source_session_id,
                    transfer_nonce=transfer_nonce,
                )
                prepare_intent = {
                    **prepare_intent,
                    "writer_complete": True,
                    "write_result": write_result,
                }
                _authorized_json_writes(
                    repo,
                    actor_session_id=actor_session_id,
                    source_session_id=source_session_id,
                    writes=((paths.prepare_intent, prepare_intent),),
                )
            raw_metrics = write_result.get("metrics")
            metrics = dict(raw_metrics) if isinstance(raw_metrics, dict) else {}
            metrics.update(
                {
                    "revision_reason": args.reason,
                    "structural_guard": write_result.get("structural_guard"),
                    "resume_ready": bool(write_result.get("resume_ready")),
                    "overflow": write_result.get("overflow"),
                    "delivery_emitted": False,
                    "deduped": bool(delivery_recent),
                    "paired_run_id": None,
                }
            )
            pointer = {
                "capsule_path": write_result.get("capsule_path"),
                "capsule_sha256": write_result.get("capsule_sha256"),
                "session_id": args.session_id,
                "session_scope": session_scope(args.session_id),
                "revision": revision,
                "resume_ready": bool(write_result.get("resume_ready")),
                "goal_identity": write_result.get("goal_identity"),
                "transfer_nonce": write_result.get("transfer_nonce"),
                "next_action": write_result.get("next_action"),
                "validation_evidence": write_result.get("validation_evidence", []),
                "resume_validation": write_result.get("resume_validation"),
                "transfer_id": None,
                "delivery": {
                    "emitted": False,
                    "deduped": delivery_recent,
                },
                "metrics": metrics,
            }
            transfer_result: dict[str, Any] | None = None
            if bool(write_result.get("resume_ready")):
                transfer_result = prepare_transfer(
                    repo,
                    source_session_id=source_session_id,
                    goal_identity=str(write_result.get("goal_identity", "")),
                    capsule_path=str(write_result.get("capsule_path", "")),
                    capsule_revision=revision,
                    capsule_sha256=str(write_result.get("capsule_sha256", "")),
                    resume_ready=True,
                    next_action=str(write_result.get("next_action", "")),
                    validation_evidence=list(write_result.get("validation_evidence") or []),
                    resume_validation_command=str(
                        (write_result.get("resume_validation") or {}).get("command", "")
                    ),
                    resume_validation_expected=str(
                        (write_result.get("resume_validation") or {}).get("expected", "")
                    ),
                    nonce=str(write_result.get("transfer_nonce", "")),
                )
                pointer["transfer_id"] = transfer_result["transfer_id"]
            revision_payload = {
                "revision": revision,
                "state_sha256": state_hash,
                "revision_reason": args.reason,
                "timestamp": time.time(),
                "goal_identity": write_result.get("goal_identity"),
                "transfer_nonce": write_result.get("transfer_nonce"),
                "next_action": write_result.get("next_action"),
                "validation_evidence": write_result.get("validation_evidence", []),
                "resume_validation": write_result.get("resume_validation"),
                "transfer_id": pointer["transfer_id"],
            }
            _authorized_json_writes(
                repo,
                actor_session_id=actor_session_id,
                source_session_id=source_session_id,
                writes=(
                    (paths.revision, revision_payload),
                    (paths.session_pointer, pointer),
                    (paths.latest_pointer, pointer),
                ),
            )
            _authorized_unlink(
                repo,
                actor_session_id=actor_session_id,
                source_session_id=source_session_id,
                path=paths.prepare_intent,
            )
            checkpoint_written = True
        else:
            revision = last_revision
            pointer = previous_pointer
            write_result = {
                "capsule_path": pointer.get("capsule_path"),
                "capsule_sha256": pointer.get("capsule_sha256"),
                "goal_identity": pointer.get("goal_identity"),
                "transfer_nonce": pointer.get("transfer_nonce"),
                "next_action": pointer.get("next_action"),
                "validation_evidence": pointer.get("validation_evidence", []),
                "resume_validation": pointer.get("resume_validation"),
                "resume_ready": pointer.get("resume_ready", False),
                "continuation_prompt": None,
                "overflow": None,
                "structural_guard": None,
                "prompt_guard": pointer.get("metrics", {}).get("prompt_guard")
                if isinstance(pointer.get("metrics"), dict)
                else None,
                "skip_reason": None,
                "metrics": pointer.get("metrics", {}),
            }
            checkpoint_written = False
            transfer_nonce = str(pointer.get("transfer_nonce", ""))

        resume_ready = bool(write_result.get("resume_ready"))
        delivery_emitted = False
        continuation_prompt = None
        if resume_ready and not delivery_recent:
            continuation_prompt = write_result.get("continuation_prompt")
            if isinstance(continuation_prompt, str) and continuation_prompt:
                delivery_emitted = True
                delivery = {
                    "timestamp": time.time(),
                    "revision": revision,
                    "capsule_path": write_result.get("capsule_path"),
                    "capsule_sha256": write_result.get("capsule_sha256"),
                    "goal_identity": write_result.get("goal_identity"),
                    "transfer_nonce": write_result.get("transfer_nonce"),
                    "transfer_id": pointer.get("transfer_id"),
                }
                _authorized_json_writes(
                    repo,
                    actor_session_id=actor_session_id,
                    source_session_id=source_session_id,
                    writes=((paths.delivery, delivery),),
                )

        raw_metrics = write_result.get("metrics")
        final_metrics = dict(raw_metrics) if isinstance(raw_metrics, dict) else {}
        final_metrics.update(
            {
                "revision_reason": args.reason,
                "structural_guard": write_result.get("structural_guard"),
                "resume_ready": resume_ready,
                "overflow": write_result.get("overflow"),
                "delivery_emitted": delivery_emitted,
                "deduped": bool(delivery_recent),
                "paired_run_id": None,
                "pointer_guard": {
                    "reused": not create_revision,
                    "failures": pointer_failures,
                },
            }
        )
        pointer = {
            "capsule_path": write_result.get("capsule_path"),
            "capsule_sha256": write_result.get("capsule_sha256"),
            "session_id": args.session_id,
            "session_scope": session_scope(args.session_id),
            "revision": revision,
            "resume_ready": resume_ready,
            "goal_identity": write_result.get("goal_identity")
            or previous_pointer.get("goal_identity"),
            "transfer_nonce": write_result.get("transfer_nonce")
            or previous_pointer.get("transfer_nonce"),
            "transfer_id": pointer.get("transfer_id")
            or previous_pointer.get("transfer_id"),
            "next_action": write_result.get("next_action")
            or previous_pointer.get("next_action"),
            "validation_evidence": write_result.get("validation_evidence")
            if "validation_evidence" in write_result
            else previous_pointer.get("validation_evidence", []),
            "resume_validation": write_result.get("resume_validation")
            or previous_pointer.get("resume_validation"),
            "delivery": {
                "emitted": delivery_emitted,
                "deduped": bool(delivery_recent),
            },
            "metrics": final_metrics,
        }
        pointer["app_guidance"] = app_capability_guidance(
            payload,
            repo=repo,
            transfer=pointer,
        )
        _authorized_json_writes(
            repo,
            actor_session_id=actor_session_id,
            source_session_id=source_session_id,
            writes=(
                (paths.session_pointer, pointer),
                (paths.latest_pointer, pointer),
            ),
        )

    result.update(
        {
            "should_handoff": delivery_emitted,
            "checkpoint_written": checkpoint_written,
            "revision_created": checkpoint_written,
            "capsule_path": write_result.get("capsule_path"),
            "capsule_sha256": write_result.get("capsule_sha256"),
            "continuation_prompt": continuation_prompt,
            "user_message": build_user_message(
                context_ratio,
                args.handoff_threshold,
                args.trigger,
            ),
            "deduped": bool(delivery_recent),
            "revision": revision,
            "resume_ready": resume_ready,
            "structural_guard": write_result.get("structural_guard"),
            "prompt_guard": write_result.get("prompt_guard"),
            "overflow": write_result.get("overflow"),
            "goal_identity": pointer.get("goal_identity"),
            "transfer_nonce": pointer.get("transfer_nonce"),
            "transfer_id": pointer.get("transfer_id"),
            "next_action": pointer.get("next_action"),
            "validation_evidence": pointer.get("validation_evidence", []),
            "resume_validation": pointer.get("resume_validation"),
            "delivery_emitted": delivery_emitted,
            "delivered": delivery_emitted,
            "skip_reason": (
                "recent delivery already emitted for this session"
                if delivery_recent
                else write_result.get("skip_reason")
            ),
            "metrics": final_metrics,
            "app_guidance": pointer["app_guidance"],
            "lifecycle_next_actions": pointer["app_guidance"]["lifecycle_next_actions"],
        }
    )
    return result


def guard_only(
    repo: Path,
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> dict[str, Any]:
    payload_session = extract_handoff_key(payload) or ""
    explicit_session = args.session_id.strip() or args.handoff_key.strip()
    args.session_id = explicit_session or payload_session
    actor_session_id = args.session_id.strip() or "default"
    source_session_id = (
        args.source_session_id.strip()
        or extract_source_session(payload)
        or discover_source_for_actor(repo, actor_session_id)
        or actor_session_id
    )
    internal = _empty_result(args, extract_context_used(payload))
    internal["_guard_repo"] = str(repo.resolve())
    internal["write_guard"] = guard_write(
        repo,
        actor_session_id=actor_session_id,
        source_session_id=source_session_id,
    )
    return internal


def main() -> int:
    args = parse_args()
    repo = resolve_repo(args.repo)
    payload: dict[str, Any] = {}
    if args.stdin_json:
        raw = sys.stdin.read()
        if raw.strip():
            try:
                decoded = json.loads(raw)
                payload = decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                payload = {}

    if not 0 <= args.handoff_threshold <= 1:
        error = {
            "should_handoff": False,
            "error": "handoff threshold must be a ratio between 0 and 1",
        }
        print(json.dumps(error, indent=2))
        return 2

    if args.official_hook_event == "PreToolUse":
        try:
            internal = guard_only(repo, args, payload)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(
            json.dumps(
                official_hook_response("PreToolUse", internal, payload),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    budget_error = byte_budget_limit_error(
        args.capsule_budget_bytes,
        args.prompt_budget_bytes,
    )
    if budget_error:
        if args.official_hook_event:
            print(json.dumps({"continue": True}))
            return 0
        error = {
            "should_handoff": False,
            "checkpoint_written": False,
            "delivery_emitted": False,
            "error": budget_error,
        }
        print(json.dumps(error, indent=2))
        return 2

    try:
        internal = invoke(repo, args, payload)
    except Exception as exc:  # Hooks fail open; direct callers receive structured evidence.
        internal = {
            "should_handoff": False,
            "checkpoint_written": False,
            "delivery_emitted": False,
            "error": str(exc),
        }
        if args.official_hook_event:
            try:
                guarded = guard_only(repo, args, payload)
                decision = guarded.get("write_guard")
                if isinstance(decision, dict) and not decision.get("allowed", False):
                    print(
                        json.dumps(
                            official_hook_response(
                                args.official_hook_event,
                                guarded,
                                payload,
                            )
                        )
                    )
                    return 0
            except Exception:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps({"continue": True}))
            return 0
        print(json.dumps(internal, indent=2))
        return 1

    output = (
        official_hook_response(args.official_hook_event, internal, payload)
        if args.official_hook_event
        else internal
    )
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
