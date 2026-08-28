"""Small JSONL client for the current Codex app-server contract."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from codex_app_jsonrpc import AppServerClient, AppServerFailure, JsonObject


CLIENT_INFO: Final[JsonObject] = {
    "name": "relay",
    "title": "Relay",
    "version": "0.6.0",
}


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    thread_id: str
    objective: str
    status: str | None
    token_budget: int | None
    title: str | None = None
    preview: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    cwd: Path
    continuation_prompt: str
    codex_binary: Path
    stderr_path: Path
    response_timeout: float = 30.0
    turn_timeout: float = 3600.0
    goal_objective: str | None = None
    goal_status: str | None = None
    goal_token_budget: int | None = None
    settings: JsonObject | None = None
    thread_name: str | None = None
    source_thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProtocolAcknowledgement:
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class ProtocolCompletion:
    acknowledgement: ProtocolAcknowledgement
    destination_readable: bool


def read_thread_goal(
    *,
    cwd: Path,
    thread_id: str,
    codex_binary: Path,
    response_timeout: float = 10.0,
) -> GoalSnapshot | None:
    """Read the persisted source Goal without resuming or forking it."""

    process = subprocess.Popen(
        [str(codex_binary), "app-server", "--stdio"],
        cwd=cwd,
        env=os.environ.copy(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        client = AppServerClient(
            process,
            response_timeout=response_timeout,
            turn_timeout=response_timeout,
        )
        client.request("initialize", _initialize_params())
        client.notify("initialized", {})
        result = client.request("thread/goal/get", {"threadId": thread_id})
        goal = _goal_snapshot(result)
        if goal is None:
            return None
        if goal.thread_id != thread_id:
            return None
        try:
            thread_result = client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
        except AppServerFailure:
            # Older app-server builds exposed Goal reads before they exposed
            # metadata reads.  The objective remains usable, but the caller
            # will use a deterministic objective fallback for the title.
            return goal
        return _goal_with_thread_metadata(goal, thread_result)
    finally:
        _stop_process(process)
        _close_streams(process)


def start_protocol(
    config: ProtocolConfig,
    on_acknowledged: Callable[[ProtocolAcknowledgement], None] | None = None,
) -> ProtocolCompletion:
    """Create, restore, start, and acknowledge one continuation turn."""

    config.stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with config.stderr_path.open("a", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            [str(config.codex_binary), "app-server", "--stdio"],
            cwd=config.cwd,
            env=os.environ.copy(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            bufsize=0,
        )
        try:
            client = AppServerClient(
                process,
                response_timeout=config.response_timeout,
                turn_timeout=config.turn_timeout,
            )
            client.request("initialize", _initialize_params())
            client.notify("initialized", {})
            thread = client.request(
                "thread/start",
                _thread_start_params(config),
            )
            thread_id = _nested_id(thread, "thread", "thread/start")
            if config.source_thread_id and thread_id == config.source_thread_id:
                raise AppServerFailure(
                    code="destination_not_fresh",
                    detail=(
                        "thread/start returned the source thread id; Relay "
                        "requires a genuinely different destination"
                    ),
                )
            _require_thread_settings(thread, config)

            if config.thread_name:
                client.request(
                    "thread/name/set",
                    {"threadId": thread_id, "name": config.thread_name},
                )

            if config.goal_objective:
                goal_params: JsonObject = {
                    "threadId": thread_id,
                    "objective": config.goal_objective,
                    # An active Goal auto-starts its own host continuation.
                    # Restore it paused so Relay's bounded prompt is the one
                    # explicit first turn.
                    "status": "paused",
                }
                if config.goal_token_budget is not None:
                    goal_params["tokenBudget"] = config.goal_token_budget
                _require_goal(
                    client.request("thread/goal/set", goal_params),
                    config,
                    expected_thread_id=thread_id,
                    expected_status="paused",
                )

            turn_params: JsonObject = {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": config.continuation_prompt,
                    }
                ],
                "cwd": str(config.cwd),
            }
            for key in (
                "approvalPolicy",
                "approvalsReviewer",
                "collaborationMode",
                "effort",
                "model",
                "permissions",
                "personality",
                "sandboxPolicy",
                "serviceTier",
                "summary",
            ):
                value = (config.settings or {}).get(key)
                if value is not None:
                    turn_params[key] = value
            turn = client.request("turn/start", turn_params)
            turn_id = _nested_id(turn, "turn", "turn/start")
            client.wait_for_started(turn_id)

            if config.goal_objective:
                _require_goal(
                    client.request(
                        "thread/goal/set",
                        {"threadId": thread_id, "status": config.goal_status or "active"},
                    ),
                    config,
                    expected_thread_id=thread_id,
                    expected_status=config.goal_status or "active",
                )
            destination_metadata = client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
            if not _thread_is_readable(
                destination_metadata,
                thread_id=thread_id,
                cwd=config.cwd,
                expected_name=config.thread_name,
            ):
                raise AppServerFailure(
                    code="destination_unreadable",
                    detail="thread/read did not verify the fresh destination",
                )

            acknowledgement = ProtocolAcknowledgement(
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if on_acknowledged is not None:
                on_acknowledged(acknowledgement)

            handed_off = False
            if config.goal_objective:
                handed_off = client.wait_for_goal_terminal(
                    thread_id,
                    handoff_state_path=_relay_state_path(config.cwd, thread_id),
                )
            else:
                client.wait_for_completion(turn_id)
            if handed_off:
                return ProtocolCompletion(
                    acknowledgement=acknowledgement,
                    destination_readable=True,
                )
            read_result = client.request(
                "thread/read",
                {
                    "threadId": thread_id,
                    "includeTurns": True,
                },
            )
            return ProtocolCompletion(
                acknowledgement=acknowledgement,
                destination_readable=_thread_is_readable(
                    read_result,
                    thread_id=thread_id,
                    cwd=config.cwd,
                    expected_name=config.thread_name,
                ),
            )
        finally:
            _stop_process(process)
            _close_streams(process)


def _goal_snapshot(result: JsonObject) -> GoalSnapshot | None:
    goal = result.get("goal")
    if not isinstance(goal, dict):
        return None
    objective = goal.get("objective")
    thread_id = goal.get("threadId")
    if (
        not isinstance(objective, str)
        or not objective.strip()
        or not isinstance(thread_id, str)
        or not thread_id
    ):
        return None
    status = goal.get("status")
    if not isinstance(status, str):
        return None
    token_budget = goal.get("tokenBudget")
    return GoalSnapshot(
        thread_id=thread_id,
        objective=objective.strip(),
        status=status,
        token_budget=(
            token_budget
            if isinstance(token_budget, int) and not isinstance(token_budget, bool)
            else None
        ),
    )


def _goal_with_thread_metadata(goal: GoalSnapshot, result: JsonObject) -> GoalSnapshot:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return goal
    title = thread.get("name")
    preview = thread.get("preview")
    thread_source = thread.get("threadSource")
    return GoalSnapshot(
        thread_id=goal.thread_id,
        objective=goal.objective,
        status=goal.status,
        token_budget=goal.token_budget,
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        preview=(
            preview.strip()
            if isinstance(preview, str) and preview.strip()
            else None
        ),
        source=_effective_thread_source(thread.get("source"), thread_source),
    )


def _require_goal(
    result: JsonObject,
    config: ProtocolConfig,
    *,
    expected_thread_id: str,
    expected_status: object,
) -> None:
    restored = _goal_snapshot(result)
    if (
        restored is None
        or restored.thread_id != expected_thread_id
        or restored.objective != config.goal_objective
        or restored.status != expected_status
        or restored.token_budget != config.goal_token_budget
    ):
        raise AppServerFailure(
            code="goal_restore_failed",
            detail="thread/goal/set did not preserve the source Goal snapshot",
        )


def _initialize_params() -> JsonObject:
    return {
        "clientInfo": CLIENT_INFO,
        "capabilities": {"experimentalApi": True},
    }


def _thread_start_params(config: ProtocolConfig) -> JsonObject:
    params: JsonObject = {
        "cwd": str(config.cwd),
        "serviceName": "relay",
    }
    settings = config.settings or {}
    for key in (
        "approvalPolicy",
        "approvalsReviewer",
        "model",
        "personality",
        "sandbox",
        "serviceTier",
        "permissions",
    ):
        value = settings.get(key)
        if value is not None:
            params[key] = value
    return params


def _thread_source(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("custom"), str):
        return f"custom:{value['custom']}"
    return None


def _effective_thread_source(source: object, thread_source: object) -> str | None:
    authoritative = _thread_source(source)
    if authoritative in {"vscode", "unknown"}:
        return authoritative
    if thread_source in {"cli", "exec"}:
        return thread_source
    if isinstance(thread_source, str) and thread_source.startswith("relay"):
        return "custom:relay"
    return authoritative


def _relay_state_path(cwd: Path, thread_id: str) -> Path:
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24]
    return cwd / ".omx" / "state" / "relay" / f"{digest}.json"


def _require_thread_settings(result: JsonObject, config: ProtocolConfig) -> None:
    settings = config.settings or {}
    for requested, effective in (
        ("approvalPolicy", "approvalPolicy"),
        ("approvalsReviewer", "approvalsReviewer"),
        ("model", "model"),
        ("serviceTier", "serviceTier"),
    ):
        expected = settings.get(requested)
        if expected is not None and result.get(effective) != expected:
            raise AppServerFailure(
                code="settings_restore_failed",
                detail=(
                    f"thread/start changed {requested} from {expected!r} "
                    f"to {result.get(effective)!r}"
                ),
            )
    sandbox = result.get("sandbox")
    expected_sandbox = settings.get("sandboxPolicy")
    if isinstance(expected_sandbox, dict):
        if not isinstance(sandbox, dict):
            raise AppServerFailure(
                code="settings_restore_failed",
                detail="thread/start omitted the requested sandboxPolicy",
            )
        for key in ("type", "networkAccess"):
            if expected_sandbox.get(key) != sandbox.get(key):
                raise AppServerFailure(
                    code="settings_restore_failed",
                    detail=f"thread/start changed sandboxPolicy.{key}",
                )
    expected_profile = settings.get("permissions")
    if isinstance(expected_profile, str):
        active = result.get("activePermissionProfile")
        if not isinstance(active, dict) or active.get("id") != expected_profile:
            raise AppServerFailure(
                code="settings_restore_failed",
                detail="thread/start did not preserve the active permission profile",
            )


def _nested_id(result: JsonObject, key: str, method: str) -> str:
    nested = result.get(key)
    if not isinstance(nested, dict):
        raise AppServerFailure(
            code="invalid_response",
            detail=f"{method} returned no {key} object",
        )
    identifier = nested.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise AppServerFailure(
            code="invalid_response",
            detail=f"{method} returned no {key} id",
        )
    return identifier


def _thread_is_readable(
    result: JsonObject,
    *,
    thread_id: str,
    cwd: Path,
    expected_name: str | None = None,
) -> bool:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return False
    if thread.get("id") != thread_id or thread.get("cwd") != str(cwd):
        return False
    return expected_name is None or thread.get("name") == expected_name


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _close_streams(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        process.stdin.close()
    if process.stdout is not None:
        process.stdout.close()
