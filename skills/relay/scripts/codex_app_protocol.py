"""Small JSONL client for the current Codex app-server contract."""

from __future__ import annotations

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
    "version": "0.4.0",
}


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    objective: str
    status: str | None
    token_budget: int | None


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    cwd: Path
    continuation_prompt: str
    codex_binary: Path
    stderr_path: Path
    response_timeout: float = 30.0
    turn_timeout: float = 3600.0
    goal_objective: str | None = None
    goal_token_budget: int | None = None


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
        client.request("initialize", {"clientInfo": CLIENT_INFO})
        client.notify("initialized", {})
        result = client.request("thread/goal/get", {"threadId": thread_id})
        return _goal_snapshot(result)
    finally:
        _stop_process(process)
        _close_streams(process)


def start_protocol(
    config: ProtocolConfig,
    on_acknowledged: Callable[[ProtocolAcknowledgement], None] | None = None,
) -> ProtocolCompletion:
    """Create a fresh thread, restore its Goal, and start one continuation turn."""

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
            client.request("initialize", {"clientInfo": CLIENT_INFO})
            client.notify("initialized", {})
            thread = client.request("thread/start", {"cwd": str(config.cwd)})
            thread_id = _nested_id(thread, "thread", "thread/start")

            if config.goal_objective:
                goal_params: JsonObject = {
                    "threadId": thread_id,
                    "objective": config.goal_objective,
                }
                if config.goal_token_budget is not None:
                    goal_params["tokenBudget"] = config.goal_token_budget
                client.request("thread/goal/set", goal_params)

            turn = client.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": config.continuation_prompt,
                        }
                    ],
                },
            )
            turn_id = _nested_id(turn, "turn", "turn/start")
            acknowledgement = ProtocolAcknowledgement(
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if on_acknowledged is not None:
                on_acknowledged(acknowledgement)

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
    if not isinstance(objective, str) or not objective.strip():
        return None
    status = goal.get("status")
    token_budget = goal.get("tokenBudget")
    return GoalSnapshot(
        objective=objective.strip(),
        status=status if isinstance(status, str) else None,
        token_budget=(
            token_budget
            if isinstance(token_budget, int) and not isinstance(token_budget, bool)
            else None
        ),
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
) -> bool:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return False
    return thread.get("id") == thread_id and thread.get("cwd") == str(cwd)


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
