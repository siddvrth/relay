#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


JsonScalar = None | bool | int | float | str
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject = dict[str, JsonValue]

_DECISION_REQUESTS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
_PERMISSIONS_REQUEST = "item/permissions/requestApproval"


@dataclass(frozen=True, slots=True)
class AppServerFailure(RuntimeError):
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


class AppServerClient:
    __slots__ = (
        "_buffer",
        "_next_id",
        "_notifications",
        "_process",
        "_response_timeout",
        "_responses",
        "_turn_timeout",
    )

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        response_timeout: float,
        turn_timeout: float,
    ) -> None:
        self._process = process
        self._response_timeout = response_timeout
        self._turn_timeout = turn_timeout
        self._next_id = 1
        self._buffer = b""
        self._notifications: list[JsonObject] = []
        self._responses: dict[int, JsonObject] = {}

    def notify(self, method: str, params: JsonObject) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: JsonObject) -> JsonObject:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self._response_timeout
        while request_id not in self._responses:
            self._route(self._read(deadline, method))
        message = self._responses.pop(request_id)
        error = message.get("error")
        if isinstance(error, dict):
            detail = error.get("message")
            raise AppServerFailure(
                code="json_rpc_error",
                detail=str(detail or error),
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise AppServerFailure(
                code="invalid_response",
                detail=f"{method} returned no result object",
            )
        return result

    def wait_for_completion(self, turn_id: str) -> None:
        pending = self._notifications
        self._notifications = []
        for message in pending:
            if self._is_completed_turn(message, turn_id):
                return
        deadline = time.monotonic() + self._turn_timeout
        while True:
            message = self._read(deadline, "turn/completed")
            if self._route(message):
                continue
            if self._is_completed_turn(message, turn_id):
                return

    def wait_for_goal_terminal(
        self,
        thread_id: str,
        *,
        handoff_state_path: Path | None = None,
    ) -> bool:
        """Wait until Goal Mode terminates or the destination hands off again."""

        deadline = time.monotonic() + self._turn_timeout
        while True:
            if handoff_state_path is not None and _handoff_acknowledged(
                handoff_state_path,
                source_thread_id=thread_id,
            ):
                return True
            result = self.request("thread/goal/get", {"threadId": thread_id})
            goal = result.get("goal")
            if not isinstance(goal, dict):
                raise AppServerFailure(
                    code="goal_unavailable",
                    detail="destination Goal disappeared before completion",
                )
            self._raise_for_failed_turn()
            status = goal.get("status")
            if status in {"complete", "blocked"}:
                return False
            if status != "active":
                raise AppServerFailure(
                    code="invalid_goal_status",
                    detail=f"destination Goal has unexpected status {status}",
                )
            if time.monotonic() >= deadline:
                raise AppServerFailure(
                    code="protocol_timeout",
                    detail="timed out waiting for the destination Goal",
                )
            time.sleep(0.25)

    def _raise_for_failed_turn(self) -> None:
        pending = self._notifications
        self._notifications = []
        for message in pending:
            if message.get("method") != "turn/completed":
                self._notifications.append(message)
                continue
            params = message.get("params")
            turn = params.get("turn") if isinstance(params, dict) else None
            status = turn.get("status") if isinstance(turn, dict) else None
            if status not in {None, "completed"}:
                raise AppServerFailure(
                    code="turn_failed",
                    detail=f"destination turn ended with status {status}",
                )

    def _route(self, message: JsonObject) -> bool:
        method = message.get("method")
        if isinstance(method, str) and "id" in message:
            self._answer_server_request(message, method)
            return True
        if isinstance(method, str):
            self._notifications.append(message)
            return False
        response_id = message.get("id")
        if type(response_id) is not int or response_id < 1:
            raise AppServerFailure(
                code="invalid_response",
                detail="app-server response has an invalid id",
            )
        self._responses[response_id] = message
        return True

    def _answer_server_request(self, message: JsonObject, method: str) -> None:
        if method in _DECISION_REQUESTS or method == _PERMISSIONS_REQUEST:
            raise AppServerFailure(
                code="approval_required",
                detail=(
                    f"destination requested {method}; Relay cannot replace the "
                    "source approval reviewer"
                ),
            )
        request_id = message.get("id")
        self._send(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Relay cannot service {method}",
                },
            }
        )

    def _send(self, message: JsonObject) -> None:
        stream = self._process.stdin
        if stream is None:
            raise AppServerFailure(
                code="process_unavailable",
                detail="app-server stdin is unavailable",
            )
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            stream.write(payload)
            stream.flush()
        except (BrokenPipeError, OSError) as error:
            raise AppServerFailure(
                code="process_died",
                detail=str(error),
            ) from error

    def _read(self, deadline: float, operation: str) -> JsonObject:
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerFailure(
                    code="protocol_timeout",
                    detail=f"timed out waiting for {operation}",
                )
            stream = self._process.stdout
            if stream is None:
                raise AppServerFailure(
                    code="process_unavailable",
                    detail="app-server stdout is unavailable",
                )
            ready, _, _ = select.select([stream.fileno()], [], [], remaining)
            if not ready:
                raise AppServerFailure(
                    code="protocol_timeout",
                    detail=f"timed out waiting for {operation}",
                )
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                code = self._process.poll()
                raise AppServerFailure(
                    code="process_died",
                    detail=f"app-server closed stdout with exit code {code}",
                )
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\n", 1)
        try:
            message = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AppServerFailure(
                code="malformed_json",
                detail=str(error),
            ) from error
        if not isinstance(message, dict):
            raise AppServerFailure(
                code="malformed_json",
                detail="app-server message is not an object",
            )
        return message

    @staticmethod
    def _is_completed_turn(message: JsonObject, turn_id: str) -> bool:
        if message.get("method") != "turn/completed":
            return False
        params = message.get("params")
        if not isinstance(params, dict):
            return False
        turn = params.get("turn")
        if not isinstance(turn, dict) or turn.get("id") != turn_id:
            return False
        status = turn.get("status")
        if status != "completed":
            raise AppServerFailure(
                code="turn_failed",
                detail=f"destination turn ended with status {status}",
            )
        return True


def _handoff_acknowledged(path: Path, *, source_thread_id: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(value, dict)
        and value.get("status") == "running"
        and value.get("source_session_id") == source_thread_id
        and isinstance(value.get("destination_thread_id"), str)
        and value["destination_thread_id"]
        and value["destination_thread_id"] != source_thread_id
    )
