"""Small JSONL client for the current Codex app-server contract."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from codex_app_jsonrpc import AppServerClient, AppServerFailure, JsonObject


CLIENT_INFO: Final[JsonObject] = {
    "name": "relay",
    "title": "Relay",
    "version": "0.5.0",
}


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    objective: str
    status: str | None
    token_budget: int | None
    tokens_used: int | None = None
    updated_at: int | None = None
    title: str | None = None
    preview: str | None = None


@dataclass(frozen=True, slots=True)
class PresentationProof:
    """Evidence that the destination was presented on the requested surface."""

    mode: str
    verified: bool
    evidence: str
    selected_thread_id: str | None = None


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
    relay_chain_id: str | None = None
    relay_sequence: int | None = None
    source_thread_id: str | None = None
    presentation_mode: str = "headless"
    presentation_timeout: float = 10.0
    presentation_ack_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ProtocolAcknowledgement:
    thread_id: str
    turn_id: str
    presentation: PresentationProof | None = None


@dataclass(frozen=True, slots=True)
class ProtocolCompletion:
    acknowledgement: ProtocolAcknowledgement
    destination_readable: bool
    presentation_verified: bool = False


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


def set_thread_goal_status(
    *,
    cwd: Path,
    thread_id: str,
    status: str,
    codex_binary: Path,
    response_timeout: float = 10.0,
) -> GoalSnapshot:
    """Change a Goal status through the app-server control plane."""

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
        result = client.request(
            "thread/goal/set",
            {"threadId": thread_id, "status": status},
        )
        goal = _goal_snapshot(result)
        if goal is None or goal.status != status:
            raise AppServerFailure(
                code="goal_status_update_failed",
                detail=f"thread/goal/set did not apply status {status!r}",
            )
        return goal
    finally:
        _stop_process(process)
        _close_streams(process)


def start_protocol(
    config: ProtocolConfig,
    on_acknowledged: Callable[[ProtocolAcknowledgement], None] | None = None,
) -> ProtocolCompletion:
    """Create, name, present, and start one continuation turn."""

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
                }
                if config.goal_token_budget is not None:
                    goal_params["tokenBudget"] = config.goal_token_budget
                if config.goal_status is not None:
                    goal_params["status"] = config.goal_status
                _require_goal(
                    client.request("thread/goal/set", goal_params),
                    config,
                    expected_status=goal_params.get("status"),
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
                "collaborationMode",
                "effort",
                "sandboxPolicy",
                "summary",
            ):
                value = (config.settings or {}).get(key)
                if value is not None:
                    turn_params[key] = value
            turn = client.request("turn/start", turn_params)
            turn_id = _nested_id(turn, "turn", "turn/start")

            acknowledgement = ProtocolAcknowledgement(
                thread_id=thread_id,
                turn_id=turn_id,
            )

            presentation = _present_destination(client, config, thread_id, turn_id)
            acknowledgement = ProtocolAcknowledgement(
                thread_id=thread_id,
                turn_id=turn_id,
                presentation=presentation,
            )
            if on_acknowledged is not None:
                on_acknowledged(acknowledgement)

            if config.goal_objective:
                client.wait_for_goal_terminal(
                    thread_id,
                    handoff_state_path=_relay_state_path(config.cwd, thread_id),
                )
            else:
                client.wait_for_completion(turn_id)
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
                presentation_verified=presentation.verified,
            )
        finally:
            _stop_process(process)
            _close_streams(process)


def _goal_snapshot(result: JsonObject) -> GoalSnapshot | None:
    goal = result.get("goal")
    if not isinstance(goal, dict):
        return None
    objective = goal.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return None
    status = goal.get("status")
    token_budget = goal.get("tokenBudget")
    tokens_used = goal.get("tokensUsed")
    updated_at = goal.get("updatedAt")
    return GoalSnapshot(
        objective=objective.strip(),
        status=status if isinstance(status, str) else None,
        token_budget=(
            token_budget
            if isinstance(token_budget, int) and not isinstance(token_budget, bool)
            else None
        ),
        tokens_used=(
            tokens_used
            if isinstance(tokens_used, int) and not isinstance(tokens_used, bool)
            else None
        ),
        updated_at=(
            updated_at
            if isinstance(updated_at, int) and not isinstance(updated_at, bool)
            else None
        ),
    )


def _goal_with_thread_metadata(goal: GoalSnapshot, result: JsonObject) -> GoalSnapshot:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return goal
    title = thread.get("name")
    preview = thread.get("preview")
    return GoalSnapshot(
        objective=goal.objective,
        status=goal.status,
        token_budget=goal.token_budget,
        tokens_used=goal.tokens_used,
        updated_at=goal.updated_at,
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        preview=(
            preview.strip()
            if isinstance(preview, str) and preview.strip()
            else None
        ),
    )


def _require_goal(
    result: JsonObject,
    config: ProtocolConfig,
    *,
    expected_status: object,
) -> None:
    restored = _goal_snapshot(result)
    if (
        restored is None
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


def _present_destination(
    client: AppServerClient,
    config: ProtocolConfig,
    thread_id: str,
    turn_id: str,
) -> PresentationProof:
    mode = config.presentation_mode.strip().lower()
    if mode in {"", "headless", "none", "cli"}:
        return PresentationProof(
            mode=mode or "headless",
            verified=True,
            evidence="presentation_not_required",
            selected_thread_id=None,
        )
    if mode != "desktop":
        raise AppServerFailure(
            code="invalid_presentation_mode",
            detail=f"unsupported presentation mode {config.presentation_mode!r}",
        )

    uri = f"codex://threads/{thread_id}"
    command = os.environ.get("RELAY_DESKTOP_PRESENTATION_COMMAND")
    if not command:
        raise AppServerFailure(
            code="desktop_focus_unsupported",
            detail=(
                "Codex app-server can create and name the destination, but it "
                "does not expose a supported Desktop focus/select request; "
                "configure RELAY_DESKTOP_PRESENTATION_COMMAND for a host "
                "integration that can return an exact presentation proof"
            ),
        )

    ack_path = config.presentation_ack_path
    if ack_path is None:
        configured = os.environ.get("RELAY_DESKTOP_PRESENTATION_ACK")
        if configured:
            ack_path = Path(configured).expanduser()
    if ack_path is None:
        raise AppServerFailure(
            code="desktop_visibility_unverified",
            detail=(
                "Desktop was asked to open the destination, but no exact "
                "presentation acknowledgement path was configured"
            ),
        )
    # A retry can reuse the deterministic state path. A proof from an earlier
    # attempt must never authorize the new destination, even if the bridge
    # fails before it can write a fresh acknowledgement.
    try:
        ack_path.unlink(missing_ok=True)
    except OSError as error:
        raise AppServerFailure(
            code="desktop_visibility_unverified",
            detail=f"could not reset Desktop presentation acknowledgement: {error}",
        ) from error
    # Reset the proof before invoking the bridge. A synchronous bridge is
    # allowed to write its proof before returning; deleting the path after the
    # command would erase valid evidence and force a false timeout.
    _run_presentation_command(
        command,
        config=config,
        thread_id=thread_id,
        turn_id=turn_id,
        uri=uri,
        ack_path=ack_path,
    )
    proof = _wait_for_presentation_ack(
        ack_path,
        thread_id=thread_id,
        turn_id=turn_id,
        chain_id=config.relay_chain_id,
        sequence=config.relay_sequence,
        source_thread_id=config.source_thread_id,
        thread_name=config.thread_name,
        timeout=config.presentation_timeout,
    )
    return PresentationProof(
        mode="desktop",
        verified=True,
        evidence=str(ack_path),
        selected_thread_id=proof["selected_thread_id"],
    )


def _run_presentation_command(
    command: str,
    *,
    config: ProtocolConfig,
    thread_id: str,
    turn_id: str,
    uri: str,
    ack_path: Path,
) -> None:
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise AppServerFailure(
            code="desktop_presentation_failed",
            detail=f"invalid Desktop presentation command: {error}",
        ) from error
    if not argv:
        raise AppServerFailure(
            code="desktop_presentation_failed",
            detail="Desktop presentation command is empty",
        )
    payload = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "chain_id": config.relay_chain_id,
        "relay_sequence": config.relay_sequence,
        "source_thread_id": config.source_thread_id,
        "thread_name": config.thread_name,
        "uri": uri,
    }
    environment = os.environ.copy()
    environment.update(
        {
            "RELAY_DESKTOP_THREAD_ID": thread_id,
            "RELAY_DESKTOP_TURN_ID": turn_id,
            "RELAY_DESKTOP_SOURCE_THREAD_ID": config.source_thread_id or "",
            "RELAY_DESKTOP_THREAD_NAME": config.thread_name or "",
            "RELAY_DESKTOP_URI": uri,
            "RELAY_DESKTOP_ACK_PATH": (
                str(ack_path)
            ),
        }
    )
    try:
        result = subprocess.run(
            argv,
            input=json.dumps(payload, separators=(",", ":")),
            capture_output=True,
            text=True,
            timeout=config.presentation_timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AppServerFailure(
            code="desktop_presentation_failed",
            detail=str(error),
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise AppServerFailure(
            code="desktop_presentation_failed",
            detail=detail,
        )


def _wait_for_presentation_ack(
    path: Path,
    *,
    thread_id: str,
    turn_id: str,
    chain_id: str | None,
    sequence: int | None,
    source_thread_id: str | None,
    thread_name: str | None,
    timeout: float,
) -> dict[str, str | None]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and _valid_presentation_ack(
            value,
            thread_id=thread_id,
            turn_id=turn_id,
            chain_id=chain_id,
            sequence=sequence,
            source_thread_id=source_thread_id,
            thread_name=thread_name,
        ):
            selected = value.get("selected_thread_id")
            return {"selected_thread_id": selected if isinstance(selected, str) else thread_id}
        time.sleep(0.05)
    raise AppServerFailure(
        code="desktop_visibility_unverified",
        detail=f"timed out waiting for Desktop to present {thread_id}",
    )


def _valid_presentation_ack(
    value: dict[str, object],
    *,
    thread_id: str,
    turn_id: str,
    chain_id: str | None,
    sequence: int | None,
    source_thread_id: str | None,
    thread_name: str | None,
) -> bool:
    if value.get("presented") is not True:
        return False
    selected = value.get("selected_thread_id")
    if selected != thread_id:
        return False
    if value.get("thread_id") != thread_id or value.get("turn_id") != turn_id:
        return False
    if chain_id is not None and value.get("chain_id") != chain_id:
        return False
    if sequence is not None and value.get("relay_sequence") != sequence:
        return False
    if source_thread_id is not None and value.get("source_thread_id") != source_thread_id:
        return False
    # Thread name is useful metadata, but the exact identity proof is the
    # destination's IDs and chain sequence. Older bridges may not echo the
    # name, so validate it only when they provide it.
    if thread_name is not None and "thread_name" in value and value.get("thread_name") != thread_name:
        return False
    return True


def _relay_state_path(cwd: Path, thread_id: str) -> Path:
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24]
    return cwd / ".omx" / "state" / "relay" / f"{digest}.json"


def _require_thread_settings(result: JsonObject, config: ProtocolConfig) -> None:
    settings = config.settings or {}
    for requested, effective in (
        ("approvalPolicy", "approvalPolicy"),
        ("approvalsReviewer", "approvalsReviewer"),
        ("model", "model"),
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
    if isinstance(sandbox, dict) and isinstance(expected_sandbox, dict):
        for key in ("type", "networkAccess"):
            if expected_sandbox.get(key) != sandbox.get(key):
                raise AppServerFailure(
                    code="settings_restore_failed",
                    detail=f"thread/start changed sandboxPolicy.{key}",
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
