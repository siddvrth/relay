"""Automatic 30% context handoff for the Relay Codex plugin."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import context_usage
from codex_app_protocol import GoalSnapshot, read_thread_goal, set_thread_goal_status
from codex_app_transport import LaunchConfig, launch, stop_worker_pid


DEFAULT_THRESHOLD = 0.30
MAX_GOAL_CHARS = 4000  # Codex's current thread/goal/set contract limit.
MAX_REQUEST_CHARS = 1200
MAX_FILE_HINT_CHARS = 1200
MAX_PROGRESS_CHARS = 2400
DEFAULT_NO_PROGRESS_LIMIT = 3
DEFAULT_FAILURE_LIMIT = 3
MAX_TITLE_CHARS = 240
TERMINAL_EVENTS = {"SessionEnd"}
CONTROL_TOOL_NAMES = {
    "goal",
    "goal_control",
    "goal-control",
    "goal/set",
    "goal/get",
    "goal/clear",
    "thread/goal/set",
    "thread/goal/get",
    "thread/goal/clear",
    "update_goal",
    "get_goal",
    "clear_goal",
    "cancel_goal",
    "stop_goal",
    "create_goal",
    "update_plan",
    "plan",
    "shutdown",
    "cancel",
    "interrupt",
    "abort",
    "quit",
    "stop",
}


def handle_hook(
    *,
    repo: Path,
    event: str,
    payload: Mapping[str, Any],
    threshold: float = DEFAULT_THRESHOLD,
    context_used: float | None = None,
    transport_enabled: bool = True,
    codex_binary: Path | None = None,
) -> dict[str, Any]:
    """Evaluate one official hook payload and return its official response."""

    if event not in {"UserPromptSubmit", "PreToolUse", *TERMINAL_EVENTS}:
        return _allow(event)

    repo = repo.resolve()

    # Terminal and control-plane operations are safety-critical.  They must
    # escape Relay before session/threshold lookup so a malformed or missing
    # session identifier cannot trap the operation that is trying to stop or
    # block the Goal.
    session_id = _session_id(payload)
    if event in TERMINAL_EVENTS:
        if session_id is None:
            return _allow(event)
        terminal_chain_id = (
            _session_chain_id(repo, session_id)
        )
        cleanup_workers(
            repo,
            session_id=None if terminal_chain_id else session_id,
            chain_id=terminal_chain_id,
        )
        return _allow(event)
    if event == "PreToolUse" and _is_control_operation(payload):
        if _is_terminal_control(payload) and session_id is not None:
            terminal_chain_id = _session_chain_id(repo, session_id)
            cleanup_workers(
                repo,
                session_id=None if terminal_chain_id else session_id,
                chain_id=terminal_chain_id,
                protect_destination_session_id=session_id,
            )
        return _allow(event)

    if session_id is None:
        return _allow(event)
    if not _valid_threshold(threshold):
        return _allow(event)

    state_path, lock_path = _state_paths(repo, session_id)

    try:
        with _locked(lock_path):
            state = _read_state(state_path)
            chain_id = (
                _string_value(state.get("relay_chain_id")) if state else None
            ) or _session_chain_id(repo, session_id)
            chain_breaker = (
                _chain_breaker_state(repo, chain_id) if chain_id else None
            )
            if (state and state.get("status") == "circuit_breaker") or chain_breaker:
                if state and state.get("status") != "circuit_breaker":
                    stopped_state = dict(state)
                    stopped_state.update(
                        {
                            "status": "circuit_breaker",
                            "circuit_breaker": (
                                chain_breaker.get("circuit_breaker")
                                if chain_breaker
                                else "repeated_handoff_failure"
                            ),
                            "error": (
                                chain_breaker.get("error")
                                if chain_breaker
                                else "Relay chain circuit breaker is active"
                            ),
                            "updated_at": _timestamp(),
                        }
                    )
                    _write_state(state_path, stopped_state)
                if chain_id:
                    cleanup_workers(
                        repo,
                        chain_id=chain_id,
                        protect_destination_session_id=session_id,
                    )
                else:
                    cleanup_workers(
                        repo,
                        session_id=session_id,
                        protect_destination_session_id=session_id,
                    )
                return _allow(event)
            pending_failure: str | None = None
            if state and state.get("status") == "running":
                pending_failure = _destination_failure(state)
                if pending_failure is None:
                    return _quiesce(event, state)
                cleanup_workers(repo, session_id=session_id)
                failed_state = dict(state)
                failed_state.update(
                    {
                        "status": "failed",
                        "error": pending_failure,
                        "failure_recorded": False,
                        "updated_at": _timestamp(),
                    }
                )
                _write_state(state_path, failed_state)
                state = failed_state
            elif state and state.get("status") == "starting":
                pending_failure = "handoff remained in starting state"
                failed_state = dict(state)
                failed_state.update(
                    {
                        "status": "failed",
                        "error": pending_failure,
                        "failure_recorded": False,
                        "updated_at": _timestamp(),
                    }
                )
                _write_state(state_path, failed_state)
                state = failed_state

            if pending_failure and state and not state.get("failure_recorded"):
                chain_id = _failure_chain_id(repo, state, session_id)
                failure_count = _record_handoff_failure(
                    repo,
                    chain_id=chain_id,
                    objective=_string_value(state.get("objective")) or "unknown",
                    original_title=_string_value(state.get("original_title")) or "Relay",
                    source_session_id=session_id,
                    source_sequence=_int_value(state.get("relay_sequence")) or 1,
                    destination_sequence=_int_value(
                        state.get("destination_relay_sequence")
                    ),
                    failure=pending_failure,
                )
                state_with_marker = dict(state)
                state_with_marker.update(
                    {
                        "relay_chain_id": chain_id,
                        "failure_recorded": True,
                        "handoff_failure_count": failure_count,
                    }
                )
                _write_state(state_path, state_with_marker)
                state = state_with_marker
                if _chain_breaker_state(repo, chain_id):
                    cleanup_workers(
                        repo,
                        chain_id=chain_id,
                        protect_destination_session_id=session_id,
                    )
                    return _allow(event)
                if failure_count >= _failure_limit():
                    _trip_circuit_breaker(
                        repo=repo,
                        state=state,
                        session_id=session_id,
                        objective=_string_value(state.get("objective")) or "unknown",
                        chain=_chain_from_state(repo, state, session_id),
                        files=_string_value(state.get("relevant_files")) or "unknown",
                        ratio=context_used if context_used is not None else 0.0,
                        threshold=threshold,
                        progress_fingerprint=_string_value(
                            state.get("progress_fingerprint")
                        )
                        or "unknown",
                        no_progress_count=_int_value(state.get("no_progress_count")) or 0,
                        failure_count=failure_count,
                        failure=pending_failure,
                        codex_binary=codex_binary,
                    )
                    return _allow(event)

            ratio = context_used
            if ratio is None:
                ratio = context_usage.extract_context_used(payload)
            if ratio is None or not _valid_ratio(ratio) or ratio < threshold:
                return _allow(event)

            if not transport_enabled:
                return _allow(event)
            binary = codex_binary or _codex_binary()
            if binary is None:
                return _allow(event)

            goal = _read_goal(repo, session_id, binary)
            if goal is None:
                return _allow(event)
            if goal.status not in (None, "active"):
                if state and state.get("relay_chain_id"):
                    cleanup_workers(
                        repo,
                        session_id=session_id,
                        protect_destination_session_id=session_id,
                    )
                    terminal_state = dict(state)
                    terminal_state.update(
                        {
                            "status": "terminal",
                            "terminal_goal_status": goal.status,
                            "updated_at": _timestamp(),
                        }
                    )
                    _write_state(state_path, terminal_state)
                return _allow(event)
            handoff = _handoff_context(payload)
            settings = _execution_settings(handoff.get("turn_context"))
            if settings is None:
                return _allow(event)
            objective = _bounded(goal.objective.strip(), MAX_GOAL_CHARS)
            objective_source = "thread/goal/get"
            chain = _resolve_chain(
                repo=repo,
                session_id=session_id,
                state=state,
                goal=goal,
                objective=objective,
            )
            request = _request(payload)
            files = _changed_files(repo)
            progress = handoff.get("recent_progress")
            progress_fingerprint = _progress_fingerprint(
                goal=goal,
                files=files,
                progress=progress if isinstance(progress, str) else None,
            )
            no_progress_count = _record_chain_progress(
                repo,
                chain_id=chain["chain_id"],
                objective=objective,
                original_title=chain["original_title"],
                source_session_id=session_id,
                source_sequence=chain["source_sequence"],
                root_thread_id=chain["root_thread_id"],
                parent_thread_id=chain["parent_thread_id"],
                progress_fingerprint=progress_fingerprint,
            )
            if _chain_breaker_state(repo, chain["chain_id"]):
                cleanup_workers(
                    repo,
                    chain_id=chain["chain_id"],
                    protect_destination_session_id=session_id,
                )
                return _allow(event)
            if no_progress_count >= _no_progress_limit():
                _trip_circuit_breaker(
                    repo=repo,
                    state=state,
                    session_id=session_id,
                    objective=objective,
                    chain=chain,
                    files=files,
                    ratio=ratio,
                    threshold=threshold,
                    progress_fingerprint=progress_fingerprint,
                    no_progress_count=no_progress_count,
                    failure_count=_chain_failure_count(repo, chain["chain_id"]),
                    failure="repeated observations with no progress marker change",
                    codex_binary=binary,
                )
                return _allow(event)
            failure_count = _chain_failure_count(repo, chain["chain_id"])
            if failure_count >= _failure_limit():
                _trip_circuit_breaker(
                    repo=repo,
                    state=state,
                    session_id=session_id,
                    objective=objective,
                    chain=chain,
                    files=files,
                    ratio=ratio,
                    threshold=threshold,
                    progress_fingerprint=progress_fingerprint,
                    no_progress_count=no_progress_count,
                    failure_count=failure_count,
                    failure="repeated handoff failures",
                    codex_binary=binary,
                )
                return _allow(event)
            destination_sequence = chain["source_sequence"] + 1
            destination_name = _relay_name(
                destination_sequence,
                chain["original_title"],
            )
            continuation = _continuation(
                repo=repo,
                source_session_id=session_id,
                objective=objective,
                request=request,
                files=files,
                progress=progress if isinstance(progress, str) else None,
                ratio=ratio,
                threshold=threshold,
                chain_id=chain["chain_id"],
                source_sequence=chain["source_sequence"],
                destination_sequence=destination_sequence,
                original_title=chain["original_title"],
            )
            presentation_mode = _presentation_mode()
            presentation_ack_path = _presentation_ack_path(
                repo,
                session_id,
                chain["chain_id"],
                destination_sequence,
            )
            _write_state(
                state_path,
                {
                    "version": 2,
                    "status": "starting",
                    "source_session_id": session_id,
                    "cwd": str(repo),
                    "objective": objective,
                    "objective_source": objective_source,
                    "next_action": "Inspect live repository state and continue the Goal.",
                    "relevant_files": files,
                    "context_used": round(ratio, 6),
                    "threshold": threshold,
                    "relay_chain_id": chain["chain_id"],
                    "root_thread_id": chain["root_thread_id"],
                    "parent_thread_id": chain["parent_thread_id"],
                    "relay_sequence": chain["source_sequence"],
                    "destination_relay_sequence": destination_sequence,
                    "original_title": chain["original_title"],
                    "destination_thread_name": destination_name,
                    "progress_fingerprint": progress_fingerprint,
                    "no_progress_count": no_progress_count,
                    "handoff_failure_count": failure_count,
                    "failure_recorded": False,
                    "presentation_mode": presentation_mode,
                    "presentation_ack_path": str(presentation_ack_path),
                    "created_at": _timestamp(),
                },
            )

            result = launch(
                LaunchConfig(
                    cwd=repo,
                    continuation_prompt=continuation,
                    codex_binary=binary,
                    goal=goal,
                    settings=settings,
                    outcome_path=state_path.with_suffix(".outcome.json"),
                    response_timeout=_timeout("RELAY_APP_SERVER_RESPONSE_TIMEOUT", 30.0),
                    turn_timeout=_timeout("RELAY_APP_SERVER_TURN_TIMEOUT", 3600.0),
                    thread_name=destination_name,
                    relay_chain_id=chain["chain_id"],
                    relay_sequence=destination_sequence,
                    source_thread_id=session_id,
                    presentation_mode=presentation_mode,
                    presentation_timeout=_timeout(
                        "RELAY_DESKTOP_PRESENTATION_TIMEOUT",
                        10.0,
                    ),
                    presentation_ack_path=presentation_ack_path,
                )
            )
            if not result.acknowledged:
                failure = result.error or "fresh thread launch failed"
                failure_count = _record_handoff_failure(
                    repo,
                    chain_id=chain["chain_id"],
                    objective=objective,
                    original_title=chain["original_title"],
                    source_session_id=session_id,
                    source_sequence=chain["source_sequence"],
                    destination_sequence=destination_sequence,
                    failure=failure,
                )
                failed_state = {
                    "version": 2,
                    "status": "failed",
                    "source_session_id": session_id,
                    "cwd": str(repo),
                    "objective": objective,
                    "objective_source": objective_source,
                    "context_used": round(ratio, 6),
                    "threshold": threshold,
                    "relay_chain_id": chain["chain_id"],
                    "root_thread_id": chain["root_thread_id"],
                    "parent_thread_id": chain["parent_thread_id"],
                    "relay_sequence": chain["source_sequence"],
                    "destination_relay_sequence": destination_sequence,
                    "original_title": chain["original_title"],
                    "destination_thread_name": destination_name,
                    "progress_fingerprint": progress_fingerprint,
                    "no_progress_count": no_progress_count,
                    "handoff_failure_count": failure_count,
                    "failure_recorded": True,
                    "presentation_mode": presentation_mode,
                    "presentation_ack_path": str(presentation_ack_path),
                    "error": failure,
                    "updated_at": _timestamp(),
                }
                if failure_count >= _failure_limit():
                    _trip_circuit_breaker(
                        repo=repo,
                        state=failed_state,
                        session_id=session_id,
                        objective=objective,
                        chain=chain,
                        files=files,
                        ratio=ratio,
                        threshold=threshold,
                        progress_fingerprint=progress_fingerprint,
                        no_progress_count=no_progress_count,
                        failure_count=failure_count,
                        failure=failure,
                        codex_binary=binary,
                    )
                else:
                    _write_state(state_path, failed_state)
                return _allow(event)

            _record_handoff_success(
                repo,
                chain_id=chain["chain_id"],
                objective=objective,
                original_title=chain["original_title"],
                source_session_id=session_id,
                source_sequence=chain["source_sequence"],
                parent_thread_id=chain["parent_thread_id"],
                destination_thread_id=result.destination_thread_id,
                destination_sequence=destination_sequence,
            )
            if _chain_breaker_state(repo, chain["chain_id"]):
                cleanup_workers(
                    repo,
                    chain_id=chain["chain_id"],
                    protect_destination_session_id=session_id,
                )
                return _allow(event)
            running_state = {
                "version": 2,
                "status": "running",
                "source_session_id": session_id,
                "destination_thread_id": result.destination_thread_id,
                "destination_turn_id": result.destination_turn_id,
                "cwd": str(repo),
                "objective": objective,
                "objective_source": objective_source,
                "next_action": "Inspect live repository state and continue the Goal.",
                "relevant_files": files,
                "context_used": round(ratio, 6),
                "threshold": threshold,
                "relay_chain_id": chain["chain_id"],
                "root_thread_id": chain["root_thread_id"],
                "parent_thread_id": chain["parent_thread_id"],
                "relay_sequence": chain["source_sequence"],
                "destination_relay_sequence": destination_sequence,
                "original_title": chain["original_title"],
                "destination_thread_name": destination_name,
                "progress_fingerprint": progress_fingerprint,
                "no_progress_count": no_progress_count,
                "handoff_failure_count": 0,
                "failure_recorded": False,
                "presentation_mode": presentation_mode,
                "presentation_status": (
                    "presented"
                    if presentation_mode == "desktop" and result.presentation_verified
                    else "not_required"
                ),
                "presentation_ack_path": str(presentation_ack_path),
                "outcome_path": str(state_path.with_suffix(".outcome.json")),
                "worker_pid": result.worker_pid,
                "created_at": state.get("created_at", _timestamp()) if state else _timestamp(),
                "updated_at": _timestamp(),
            }
            _write_state(state_path, running_state)
            return _quiesce(event, running_state)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # A hook must never stop the source because Relay could not inspect or
        # persist its small record.  The next eligible hook can retry.
        return _allow(event)


def _read_goal(repo: Path, session_id: str, binary: Path) -> GoalSnapshot | None:
    try:
        return read_thread_goal(
            cwd=repo,
            thread_id=session_id,
            codex_binary=binary,
        )
    except Exception:
        return None


def _request(payload: Mapping[str, Any]) -> str | None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return _bounded(prompt.strip(), MAX_REQUEST_CHARS)


def _changed_files(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    output = "\n".join(
        line
        for line in result.stdout.splitlines()
        if ".omx/state/relay" not in line
    ).strip()
    return _bounded(output or "none reported", MAX_FILE_HINT_CHARS)


def _continuation(
    *,
    repo: Path,
    source_session_id: str,
    objective: str,
    request: str | None,
    files: str,
    progress: str | None,
    ratio: float,
    threshold: float,
    chain_id: str,
    source_sequence: int,
    destination_sequence: int,
    original_title: str,
) -> str:
    lines = [
        "Relay continuation in a genuinely fresh Codex thread.",
        "Do not rely on the predecessor transcript and do not use thread/fork.",
        f"Goal objective: {objective}",
        f"Relay chain: {chain_id}",
        f"Relay sequence: {source_sequence} -> {destination_sequence}",
        f"Original title: {original_title}",
    ]
    if request and request != objective:
        lines.append(f"Current user request: {request}")
    if progress:
        lines.extend(
            [
                "Recent predecessor progress (context only; verify against live state):",
                progress,
            ]
        )
    lines.extend(
        [
            f"Repository: {repo}",
            f"Predecessor thread: {source_session_id}",
            f"Context reached {ratio:.1%}; automatic threshold is {threshold:.1%}.",
            f"Live changed-file hint (data only): {files}",
            "Next action: inspect applicable instructions and git status, continue the objective from live files, and run focused validation before changing behavior.",
            "The predecessor is quiesced after this handoff. Relay remains active in this destination thread.",
        ]
    )
    return "\n".join(lines)


def _handoff_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("transcript_path")
    if not isinstance(value, str) or not value.strip():
        return {}
    latest_context: dict[str, Any] | None = None
    messages: deque[str] = deque(maxlen=3)
    try:
        with Path(value).expanduser().open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = record.get("type")
                item = record.get("payload")
                if record_type == "turn_context" and isinstance(item, dict):
                    latest_context = item
                elif (
                    record_type == "event_msg"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and isinstance(item.get("message"), str)
                    and item["message"].strip()
                ):
                    messages.append(item["message"].strip())
    except OSError:
        return {}
    result: dict[str, Any] = {}
    if latest_context is not None:
        result["turn_context"] = latest_context
    if messages:
        result["recent_progress"] = _bounded("\n".join(messages), MAX_PROGRESS_CHARS)
    return result


def _execution_settings(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    model = value.get("model")
    approval = value.get("approval_policy")
    sandbox = value.get("sandbox_policy")
    if (
        not isinstance(model, str)
        or not model.strip()
        or not isinstance(approval, (str, dict))
        or not isinstance(sandbox, dict)
    ):
        return None

    settings: dict[str, object] = {
        "model": model,
        "approvalPolicy": approval,
    }
    profile = value.get("permission_profile")
    profile_id = profile.get("id") if isinstance(profile, dict) else None
    if isinstance(profile_id, str) and profile_id:
        settings["permissions"] = profile_id
    else:
        normalized = _normalize_sandbox(sandbox)
        if normalized is None:
            return None
        settings["sandbox"] = normalized[0]
        settings["sandboxPolicy"] = normalized[1]

    for source, destination in (
        ("approvals_reviewer", "approvalsReviewer"),
        ("collaboration_mode", "collaborationMode"),
        ("effort", "effort"),
        ("personality", "personality"),
        ("service_tier", "serviceTier"),
        ("summary", "summary"),
    ):
        setting = value.get(source)
        if setting is not None:
            settings[destination] = setting
    return settings


def _normalize_sandbox(value: Mapping[str, Any]) -> tuple[str, dict[str, object]] | None:
    source_type = value.get("type")
    if not isinstance(source_type, str):
        return None
    types = {
        "danger-full-access": ("danger-full-access", "dangerFullAccess"),
        "read-only": ("read-only", "readOnly"),
        "workspace-write": ("workspace-write", "workspaceWrite"),
        "external-sandbox": ("danger-full-access", "externalSandbox"),
    }
    mapped = types.get(source_type)
    if mapped is None:
        return None
    key_names = {
        "exclude_slash_tmp": "excludeSlashTmp",
        "exclude_tmpdir_env_var": "excludeTmpdirEnvVar",
        "network_access": "networkAccess",
        "read_only_access": "readOnlyAccess",
        "readable_roots": "readableRoots",
        "writable_roots": "writableRoots",
        "include_platform_defaults": "includePlatformDefaults",
    }

    def convert(item: object) -> object:
        if isinstance(item, dict):
            return {key_names.get(str(key), str(key)): convert(child) for key, child in item.items()}
        if isinstance(item, list):
            return [convert(child) for child in item]
        return item

    policy = convert(value)
    if not isinstance(policy, dict):
        return None
    policy["type"] = mapped[1]
    return mapped[0], policy


def _presentation_mode() -> str:
    value = os.environ.get("RELAY_PRESENTATION_MODE")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    if os.environ.get("RELAY_DESKTOP_HANDOFF") == "1":
        return "desktop"
    originator = os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "")
    if "desktop" in originator.lower():
        # Desktop must not silently accept a backend-only handoff. Without a
        # host presentation bridge, _present_destination fails open and leaves
        # the source usable instead of quiescing an invisible successor.
        return "desktop"
    return "headless"


def _presentation_ack_path(
    repo: Path,
    source_session_id: str,
    chain_id: str,
    sequence: int,
) -> Path:
    digest = hashlib.sha256(
        f"{source_session_id}\0{chain_id}\0{sequence}".encode("utf-8")
    ).hexdigest()[:24]
    return repo / ".omx" / "state" / "relay" / f"{digest}.presentation.json"


def _resolve_chain(
    *,
    repo: Path,
    session_id: str,
    state: Mapping[str, Any] | None,
    goal: GoalSnapshot,
    objective: str,
) -> dict[str, Any]:
    parent = _find_parent_state(repo, session_id)
    chain_id = _string_value(
        state.get("relay_chain_id") if state else None
    ) or _string_value(parent.get("relay_chain_id") if parent else None)

    original_title = (
        _string_value(state.get("original_title")) if state else None
    ) or (_string_value(parent.get("original_title")) if parent else None)
    if not original_title:
        original_title = goal.title or goal.preview or objective
        original_title = _strip_relay_name(original_title)

    if not chain_id:
        seed = json.dumps(
            {
                "source_session_id": (
                    _string_value(parent.get("source_session_id"))
                    if parent
                    else session_id
                ),
                "objective": objective,
                "original_title": original_title,
                "cwd": str(repo),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        chain_id = "relay-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]

    source_sequence = _int_value(state.get("relay_sequence")) if state else None
    if source_sequence is None and parent is not None:
        source_sequence = _int_value(parent.get("destination_relay_sequence"))
        if source_sequence is None:
            parent_sequence = _int_value(parent.get("relay_sequence"))
            source_sequence = parent_sequence + 1 if parent_sequence is not None else None
    if source_sequence is None:
        source_sequence = 1
    root_thread_id = (
        _string_value(state.get("root_thread_id")) if state else None
    ) or (_string_value(parent.get("root_thread_id")) if parent else None)
    if root_thread_id is None:
        root_thread_id = (
            _string_value(parent.get("source_session_id")) if parent else session_id
        )
    parent_thread_id = (
        _string_value(state.get("parent_thread_id")) if state else None
    ) or (_string_value(parent.get("source_session_id")) if parent else None)
    return {
        "chain_id": chain_id,
        "original_title": _bounded(original_title.strip(), MAX_TITLE_CHARS),
        "source_sequence": source_sequence,
        "root_thread_id": root_thread_id,
        "parent_thread_id": parent_thread_id,
    }


def _find_parent_state(repo: Path, destination_session_id: str) -> dict[str, Any] | None:
    for path in _state_files(repo):
        value = _read_state(path)
        if not value:
            continue
        if value.get("destination_thread_id") == destination_session_id:
            return value
    return None


def _session_chain_id(repo: Path, session_id: str) -> str | None:
    state_path, _ = _state_paths(repo, session_id)
    state = _read_state(state_path)
    chain_id = _string_value(state.get("relay_chain_id")) if state else None
    if chain_id:
        return chain_id
    parent = _find_parent_state(repo, session_id)
    return _string_value(parent.get("relay_chain_id")) if parent else None


def _chain_breaker_state(repo: Path, chain_id: str) -> dict[str, Any] | None:
    path, lock_path = _chain_paths(repo, chain_id)
    with _locked(lock_path):
        state = _read_state(path)
    return state if state and state.get("status") == "circuit_breaker" else None


def _record_chain_progress(
    repo: Path,
    *,
    chain_id: str,
    objective: str,
    original_title: str,
    source_session_id: str,
    source_sequence: int,
    root_thread_id: str,
    parent_thread_id: str | None,
    progress_fingerprint: str,
) -> int:
    path, lock_path = _chain_paths(repo, chain_id)
    with _locked(lock_path):
        previous = _read_state(path) or {}
        previous_fingerprint = _string_value(previous.get("progress_fingerprint"))
        previous_count = _int_value(previous.get("no_progress_count")) or 0
        previous_source = _string_value(previous.get("source_session_id"))
        failure_count = _int_value(previous.get("handoff_failure_count")) or 0
        if previous.get("status") == "circuit_breaker":
            return previous_count
        if previous_source == source_session_id:
            count = previous_count
        elif previous_fingerprint and previous_fingerprint == progress_fingerprint:
            count = previous_count + 1
        else:
            count = 0
        _write_state(
            path,
            {
                "version": 1,
                "status": "active",
                "relay_chain_id": chain_id,
                "objective": objective,
                "original_title": original_title,
                "source_session_id": source_session_id,
                "source_sequence": source_sequence,
                "root_thread_id": root_thread_id,
                "parent_thread_id": parent_thread_id,
                "progress_fingerprint": progress_fingerprint,
                "no_progress_count": count,
                "handoff_failure_count": failure_count,
                "updated_at": _timestamp(),
            },
        )
        return count


def _failure_limit() -> int:
    value = os.environ.get("RELAY_FAILURE_LIMIT")
    try:
        parsed = int(value) if value is not None else DEFAULT_FAILURE_LIMIT
    except ValueError:
        return DEFAULT_FAILURE_LIMIT
    return parsed if parsed > 0 else DEFAULT_FAILURE_LIMIT


def _failure_chain_id(
    repo: Path,
    state: Mapping[str, Any],
    session_id: str,
) -> str:
    existing = _string_value(state.get("relay_chain_id"))
    if existing:
        return existing
    seed = "\0".join(
        (
            str(repo),
            session_id,
            _string_value(state.get("destination_thread_id")) or "",
        )
    )
    return "relay-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _chain_from_state(
    repo: Path,
    state: Mapping[str, Any],
    session_id: str,
) -> dict[str, Any]:
    parent = _find_parent_state(repo, session_id)
    chain_id = _failure_chain_id(repo, state, session_id)
    original_title = _string_value(state.get("original_title")) or (
        _string_value(parent.get("original_title")) if parent else None
    ) or "Relay"
    source_sequence = _int_value(state.get("relay_sequence")) or 1
    root_thread_id = _string_value(state.get("root_thread_id")) or (
        _string_value(parent.get("root_thread_id")) if parent else None
    ) or (_string_value(parent.get("source_session_id")) if parent else None) or session_id
    parent_thread_id = _string_value(state.get("parent_thread_id")) or (
        _string_value(parent.get("source_session_id")) if parent else None
    )
    return {
        "chain_id": chain_id,
        "original_title": _bounded(original_title, MAX_TITLE_CHARS),
        "source_sequence": source_sequence,
        "root_thread_id": root_thread_id,
        "parent_thread_id": parent_thread_id,
    }


def _chain_failure_count(repo: Path, chain_id: str) -> int:
    path, lock_path = _chain_paths(repo, chain_id)
    with _locked(lock_path):
        state = _read_state(path)
        return _int_value(state.get("handoff_failure_count")) or 0 if state else 0


def _record_handoff_failure(
    repo: Path,
    *,
    chain_id: str,
    objective: str,
    original_title: str,
    source_session_id: str,
    source_sequence: int,
    destination_sequence: int | None,
    failure: str,
) -> int:
    path, lock_path = _chain_paths(repo, chain_id)
    with _locked(lock_path):
        previous = _read_state(path) or {}
        if previous.get("status") == "circuit_breaker":
            return _int_value(previous.get("handoff_failure_count")) or 0
        count = (_int_value(previous.get("handoff_failure_count")) or 0) + 1
        _write_state(
            path,
            {
                "version": 1,
                "status": "active",
                "relay_chain_id": chain_id,
                "objective": objective,
                "original_title": original_title,
                "source_session_id": source_session_id,
                "source_sequence": source_sequence,
                "root_thread_id": _string_value(previous.get("root_thread_id"))
                or source_session_id,
                "parent_thread_id": _string_value(previous.get("parent_thread_id")),
                "progress_fingerprint": _string_value(
                    previous.get("progress_fingerprint")
                ),
                "no_progress_count": _int_value(previous.get("no_progress_count")) or 0,
                "handoff_failure_count": count,
                "last_failure": _bounded(failure, MAX_PROGRESS_CHARS),
                "last_failure_destination_sequence": destination_sequence,
                "updated_at": _timestamp(),
            },
        )
        return count


def _record_handoff_success(
    repo: Path,
    *,
    chain_id: str,
    objective: str,
    original_title: str,
    source_session_id: str,
    source_sequence: int,
    parent_thread_id: str | None,
    destination_thread_id: str | None,
    destination_sequence: int,
) -> None:
    path, lock_path = _chain_paths(repo, chain_id)
    with _locked(lock_path):
        previous = _read_state(path) or {}
        if previous.get("status") == "circuit_breaker":
            return
        _write_state(
            path,
            {
                "version": 1,
                "status": "active",
                "relay_chain_id": chain_id,
                "objective": objective,
                "original_title": original_title,
                "source_session_id": source_session_id,
                "source_sequence": source_sequence,
                "root_thread_id": _string_value(previous.get("root_thread_id"))
                or source_session_id,
                "parent_thread_id": parent_thread_id
                or _string_value(previous.get("parent_thread_id")),
                "progress_fingerprint": _string_value(
                    previous.get("progress_fingerprint")
                ),
                "no_progress_count": _int_value(previous.get("no_progress_count")) or 0,
                "handoff_failure_count": 0,
                "destination_thread_id": destination_thread_id,
                "last_destination_thread_id": destination_thread_id,
                "last_destination_sequence": destination_sequence,
                "updated_at": _timestamp(),
            },
        )


def _mark_chain_circuit_breaker(
    repo: Path,
    *,
    objective: str,
    chain: Mapping[str, Any],
    session_id: str,
    progress_fingerprint: str,
    no_progress_count: int,
    failure_count: int,
    failure: str,
) -> None:
    path, lock_path = _chain_paths(repo, str(chain["chain_id"]))
    with _locked(lock_path):
        previous = _read_state(path) or {}
        updated = dict(previous)
        updated.update(
            {
                "version": 1,
                "status": "circuit_breaker",
                "relay_chain_id": chain["chain_id"],
                "objective": objective,
                "original_title": chain["original_title"],
                "source_session_id": session_id,
                "source_sequence": chain["source_sequence"],
                "root_thread_id": chain["root_thread_id"],
                "parent_thread_id": chain["parent_thread_id"],
                "progress_fingerprint": progress_fingerprint,
                "no_progress_count": no_progress_count,
                "handoff_failure_count": failure_count,
                "circuit_breaker": (
                    "repeated_no_progress"
                    if "no progress" in failure
                    else "repeated_handoff_failure"
                ),
                "error": (
                    "Relay stopped automatic handoff after repeated "
                    f"{failure}"
                ),
                "updated_at": _timestamp(),
            }
        )
        _write_state(path, updated)


def _trip_circuit_breaker(
    *,
    repo: Path,
    state: Mapping[str, Any] | None,
    session_id: str,
    objective: str,
    chain: Mapping[str, Any],
    files: str,
    ratio: float,
    threshold: float,
    progress_fingerprint: str,
    no_progress_count: int,
    failure_count: int,
    failure: str,
    codex_binary: Path | None,
) -> None:
    circuit_state = _circuit_breaker_state(
        state=state,
        session_id=session_id,
        objective=objective,
        chain=chain,
        files=files,
        ratio=ratio,
        threshold=threshold,
        progress_fingerprint=progress_fingerprint,
        no_progress_count=no_progress_count,
        failure_count=failure_count,
        failure=failure,
    )
    circuit_state["cwd"] = str(repo)
    _mark_chain_circuit_breaker(
        repo,
        objective=objective,
        chain=chain,
        session_id=session_id,
        progress_fingerprint=progress_fingerprint,
        no_progress_count=no_progress_count,
        failure_count=failure_count,
        failure=failure,
    )
    state_path, _ = _state_paths(repo, session_id)
    _write_state(state_path, circuit_state)

    binary = codex_binary or _codex_binary()
    updated = dict(circuit_state)
    if binary is None:
        updated["goal_status"] = "block_unavailable"
    else:
        try:
            set_thread_goal_status(
                cwd=repo,
                thread_id=session_id,
                status="blocked",
                codex_binary=binary,
            )
        except Exception as error:
            updated["goal_status"] = "block_failed"
            updated["goal_status_error"] = _bounded(str(error), MAX_PROGRESS_CHARS)
        else:
            updated["goal_status"] = "blocked"
    _write_state(state_path, updated)

    # Mark the current Goal blocked before cleaning the chain. The worker that
    # owns this destination is allowed to observe that terminal state and exit
    # normally; killing it first would also kill the hook that is performing
    # the safety transition. Older/other chain workers are still terminated.
    cleanup_workers(
        repo,
        chain_id=chain["chain_id"],
        protect_destination_session_id=session_id,
    )


def _circuit_breaker_state(
    *,
    state: Mapping[str, Any] | None,
    session_id: str,
    objective: str,
    chain: Mapping[str, Any],
    files: str,
    ratio: float,
    threshold: float,
    progress_fingerprint: str,
    no_progress_count: int,
    failure_count: int,
    failure: str,
) -> dict[str, Any]:
    result = dict(state or {})
    result.update(
        {
            "version": 2,
            "status": "circuit_breaker",
            "source_session_id": session_id,
            "objective": objective,
            "objective_source": "thread/goal/get",
            "cwd": result.get("cwd"),
            "relay_chain_id": chain["chain_id"],
            "root_thread_id": chain["root_thread_id"],
            "parent_thread_id": chain["parent_thread_id"],
            "relay_sequence": chain["source_sequence"],
            "original_title": chain["original_title"],
            "relevant_files": files,
            "context_used": round(ratio, 6),
            "threshold": threshold,
            "progress_fingerprint": progress_fingerprint,
            "no_progress_count": no_progress_count,
            "handoff_failure_count": failure_count,
            "circuit_breaker": (
                "repeated_no_progress"
                if "no progress" in failure
                else "repeated_handoff_failure"
            ),
            "error": (
                "Relay stopped automatic handoff after repeated "
                f"{failure}"
            ),
            "updated_at": _timestamp(),
        }
    )
    return result


def _progress_fingerprint(
    *,
    goal: GoalSnapshot,
    files: str,
    progress: str | None,
) -> str:
    value = json.dumps(
        {
            "objective": goal.objective,
            "status": goal.status,
            "tokens_used": goal.tokens_used,
            "changed_files": files,
            "recent_progress": progress or "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _no_progress_limit() -> int:
    value = os.environ.get("RELAY_NO_PROGRESS_LIMIT")
    try:
        parsed = int(value) if value is not None else DEFAULT_NO_PROGRESS_LIMIT
    except ValueError:
        return DEFAULT_NO_PROGRESS_LIMIT
    return parsed if parsed > 0 else DEFAULT_NO_PROGRESS_LIMIT


def _relay_name(sequence: int, original_title: str) -> str:
    label = _roman(sequence)
    return _bounded(f"Relay {label}: {original_title}", MAX_TITLE_CHARS)


def _roman(value: int) -> str:
    if value <= 0 or value >= 4000:
        return str(value)
    result: list[str] = []
    for number, token in (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    ):
        count, value = divmod(value, number)
        result.append(token * count)
    return "".join(result)


def _strip_relay_name(value: str) -> str:
    stripped = re.sub(r"^Relay\s+[IVXLCDM0-9]+\s*:\s*", "", value.strip())
    return stripped or value.strip()


def cleanup_workers(
    repo: Path,
    *,
    session_id: str | None = None,
    chain_id: str | None = None,
    protect_destination_session_id: str | None = None,
) -> dict[str, Any]:
    """Stop every matching detached worker and record a terminal outcome."""

    cleaned: list[int] = []
    skipped: list[int] = []
    matching_states: list[tuple[Path, dict[str, Any]]] = []
    worker_pids: set[int] = set()
    for path in _state_files(repo):
        state = _read_state(path)
        if not state:
            continue
        if session_id is not None and state.get("source_session_id") != session_id:
            continue
        if chain_id is not None and state.get("relay_chain_id") != chain_id:
            continue
        if (
            protect_destination_session_id is not None
            and state.get("destination_thread_id") == protect_destination_session_id
        ):
            # This state owns the worker that is currently running the hook.
            # Leave both its outcome and process group alone so a preceding
            # Goal transition can let that worker shut down cleanly.
            continue
        matching_states.append((path, state))
        pid = _int_value(state.get("worker_pid"))
        outcome_path = state.get("outcome_path")
        outcome: dict[str, Any] | None = None
        if isinstance(outcome_path, str):
            outcome = _read_state(Path(outcome_path))
            if outcome is None or outcome.get("status") == "running":
                _write_state(
                    Path(outcome_path),
                    {
                        "status": "cancelled",
                        "error": "Relay worker cancelled by terminal source path",
                        "worker_pid": pid,
                        "thread_id": state.get("destination_thread_id"),
                        "turn_id": state.get("destination_turn_id"),
                    },
                )
        if pid is not None and (
            state.get("status") in {"starting", "running"}
            or (outcome is not None and outcome.get("status") == "running")
        ):
            worker_pids.add(pid)
    worker_pids.update(
        _worker_pids(repo, chain_id=chain_id, session_id=session_id)
    )
    for pid in sorted(worker_pids):
        if stop_worker_pid(pid, repo=repo):
            cleaned.append(pid)
        else:
            skipped.append(pid)
    for path, state in matching_states:
        if state.get("status") in {"starting", "running"}:
            updated = dict(state)
            updated.update(
                {
                    "status": "cancelled",
                    "cleanup": (
                        "worker_terminated"
                        if any(pid in cleaned for pid in worker_pids)
                        else "worker_not_found"
                    ),
                    "updated_at": _timestamp(),
                }
            )
            _write_state(path, updated)
    _remove_orphaned_request_files(repo)
    return {"cleaned": cleaned, "skipped": skipped}


def _worker_pids(
    repo: Path,
    *,
    chain_id: str | None,
    session_id: str | None,
) -> set[int]:
    """Find detached Relay workers even when a hook died before persisting its PID."""

    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    found: set[int] = set()
    repo_text = str(repo.resolve())
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        command = fields[1]
        if (
            "codex_app_transport.py" not in command
            or "--worker-request" not in command
            or repo_text not in command
        ):
            continue
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        try:
            request_path = Path(argv[argv.index("--worker-request") + 1])
        except (ValueError, IndexError):
            continue
        request = _read_state(request_path)
        if chain_id is not None and (
            not request or request.get("relay_chain_id") != chain_id
        ):
            continue
        if session_id is not None and (
            not request or request.get("source_thread_id") != session_id
        ):
            continue
        found.add(pid)
    return found


def _remove_orphaned_request_files(repo: Path) -> None:
    """Remove request envelopes left by workers killed before their finally block."""

    root = repo / ".omx" / "state" / "relay"
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    active_requests: set[str] = set()
    for line in result.stdout.splitlines():
        if "codex_app_transport.py" not in line or "--worker-request" not in line:
            continue
        try:
            argv = shlex.split(line)
            active_requests.add(argv[argv.index("--worker-request") + 1])
        except (ValueError, IndexError):
            continue
    try:
        candidates = list(root.glob(".request-*.json"))
    except OSError:
        return
    for path in candidates:
        if str(path) in active_requests:
            continue
        path.unlink(missing_ok=True)


def _session_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_control_operation(payload: Mapping[str, Any]) -> bool:
    names: list[str] = []
    for key in (
        "tool_name",
        "operation",
        "method",
        "command_name",
        "action",
        "status",
        "state",
    ):
        value = payload.get(key)
        if isinstance(value, str):
            names.append(value)
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in (
            "operation",
            "method",
            "action",
            "command",
            "name",
            "status",
            "state",
        ):
            value = tool_input.get(key)
            if isinstance(value, str):
                names.append(value)
    terminal_tokens = {
        "abort",
        "blocked",
        "cancel",
        "complete",
        "completed",
        "exit",
        "interrupt",
        "quit",
        "stop",
        "terminate",
    }
    for raw in names:
        normalized = (
            raw.strip()
            .lower()
            .replace("::", "/")
            .replace(".", "/")
            .replace(" ", "_")
        )
        namespaced = {normalized, normalized.rsplit("/", 1)[-1]}
        if namespaced & CONTROL_TOOL_NAMES:
            return True
        if "goal" in normalized and any(
            token in normalized
            for token in (
                "set",
                "get",
                "clear",
                "cancel",
                "stop",
                "update",
                "complete",
                "block",
                "transition",
                "terminate",
            )
        ):
            return True
        # Do not classify arbitrary shell text such as `echo complete` as a
        # control operation. Only an exact terminal operation is an escape
        # hatch; Goal actions/statuses are handled by the control names above.
        if normalized in terminal_tokens:
            return True
    return False


def _is_terminal_control(payload: Mapping[str, Any]) -> bool:
    values: list[str] = []
    for key in ("tool_name", "operation", "method", "command_name"):
        value = payload.get(key)
        if isinstance(value, str):
            values.append(value)
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("operation", "method", "action", "command", "status", "state"):
            value = tool_input.get(key)
            if isinstance(value, str):
                values.append(value)
    terminal_tokens = {
        "cancel",
        "cancelled",
        "canceled",
        "complete",
        "completed",
        "blocked",
        "shutdown",
        "interrupt",
        "abort",
        "quit",
        "stop",
        "exit",
        "terminate",
    }
    return any(
        token in terminal_tokens
        for value in values
        for token in re.split(r"[^a-z0-9]+", value.lower())
        if token
    )


def _state_files(repo: Path) -> list[Path]:
    root = repo / ".omx" / "state" / "relay"
    try:
        paths = list(root.glob("*.json"))
    except OSError:
        return []
    return [
        path
        for path in paths
        if not path.name.startswith("chain-")
        and not path.name.endswith(".outcome.json")
        and not path.name.endswith(".presentation.json")
    ]


def _chain_paths(repo: Path, chain_id: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(chain_id.encode("utf-8")).hexdigest()[:24]
    root = repo / ".omx" / "state" / "relay"
    return root / f"chain-{digest}.json", root / f"chain-{digest}.lock"


def _string_value(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int_value(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _destination_failure(state: Mapping[str, Any]) -> str | None:
    value = state.get("outcome_path")
    outcome: dict[str, Any] | None = None
    if isinstance(value, str):
        outcome = _read_state(Path(value))
    if outcome is not None:
        status = outcome.get("status")
        if status == "failed":
            return str(outcome.get("error") or "destination worker failed")
        if status == "completed":
            return None
    pid = state.get("worker_pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        if not _pid_is_alive(pid):
            return "destination worker exited before reporting completion"
    return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    status = result.stdout.strip()
    return bool(status) and not status.startswith("Z")


def _quiesce(event: str, state: Mapping[str, Any]) -> dict[str, Any]:
    thread_id = state.get("destination_thread_id")
    suffix = f" Destination thread: {thread_id}." if thread_id else ""
    reason = "Relay started a fresh successor thread; the predecessor is quiesced." + suffix
    if event == "UserPromptSubmit":
        return {
            "continue": False,
            "stopReason": reason,
            "decision": "block",
            "reason": reason,
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow(event: str) -> dict[str, Any]:
    return {} if event == "PreToolUse" or event in TERMINAL_EVENTS else {"continue": True}


def _codex_binary() -> Path | None:
    configured = os.environ.get("RELAY_CODEX_BINARY")
    value = configured or shutil.which("codex")
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    return path if path.is_file() else None


def _state_paths(repo: Path, session_id: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    root = repo / ".omx" / "state" / "relay"
    return root / f"{digest}.json", root / f"{digest}.lock"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _write_state(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def _bounded(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit]


def _valid_threshold(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and 0 <= float(value) <= 1


def _valid_ratio(value: object) -> bool:
    return _valid_threshold(value)


def _timeout(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--stdin-json", action="store_true")
    parser.add_argument("--handoff-threshold", "--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument(
        "--official-hook-event",
        choices=("UserPromptSubmit", "PreToolUse", *sorted(TERMINAL_EVENTS)),
        required=True,
    )
    parser.add_argument("--context-used", type=float)
    args = parser.parse_args()
    payload = _payload() if args.stdin_json else {}
    response = handle_hook(
        repo=args.repo,
        event=args.official_hook_event,
        payload=payload,
        threshold=args.handoff_threshold,
        context_used=args.context_used,
        transport_enabled=os.environ.get("RELAY_CODEX_APP_TRANSPORT") != "disabled",
    )
    print(json.dumps(response, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
