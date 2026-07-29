#!/usr/bin/env python3
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
    "version": "0.3.0",
}


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    cwd: Path
    continuation_prompt: str
    codex_binary: Path
    stderr_path: Path
    response_timeout: float
    turn_timeout: float


@dataclass(frozen=True, slots=True)
class ProtocolAcknowledgement:
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class ProtocolCompletion:
    acknowledgement: ProtocolAcknowledgement
    destination_readable: bool


def start_protocol(
    config: ProtocolConfig,
    on_acknowledged: Callable[[ProtocolAcknowledgement], None] | None = None,
) -> ProtocolCompletion:
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
            readable = _thread_is_readable(
                read_result,
                thread_id=thread_id,
                cwd=config.cwd,
            )
            return ProtocolCompletion(
                acknowledgement=acknowledgement,
                destination_readable=readable,
            )
        finally:
            _stop_process(process)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


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
