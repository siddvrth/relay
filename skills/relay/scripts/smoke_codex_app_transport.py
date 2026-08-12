#!/usr/bin/env python3
"""Smoke the real local Codex app-server fresh-thread path."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
from pathlib import Path

from codex_app_jsonrpc import AppServerClient
from codex_app_protocol import CLIENT_INFO, GoalSnapshot
from codex_app_transport import LaunchConfig, launch


def main() -> int:
    codex = shutil.which("codex")
    if codex is None:
        print(json.dumps({"ok": False, "skipped": True, "reason": "codex not found"}))
        return 2

    with tempfile.TemporaryDirectory(prefix="relay-app-smoke-") as temporary:
        repo = Path(temporary).resolve()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        try:
            return _run_smoke(Path(codex).resolve(), repo)
        finally:
            _stop_relay_workers(repo)


def _run_smoke(codex: Path, repo: Path) -> int:
    result = launch(
        LaunchConfig(
            cwd=repo,
            continuation_prompt=(
                "Relay smoke test in a fresh thread. Reply exactly "
                "RELAY_SMOKE_ACK, then stop. Do not modify files."
            ),
            codex_binary=codex,
            goal=GoalSnapshot(
                objective="Verify the Relay fresh-thread app-server smoke path.",
                status="active",
                token_budget=None,
            ),
            response_timeout=30.0,
            turn_timeout=120.0,
        ),
        acknowledgement_timeout=30.0,
    )
    thread = _read_thread(codex, repo, result.destination_thread_id)
    goal = _read_goal(codex, repo, result.destination_thread_id)
    ok = bool(
        result.acknowledged
        and result.destination_thread_id
        and result.destination_turn_id
        and thread.get("id") == result.destination_thread_id
        and thread.get("cwd") == str(repo)
        and goal.get("objective")
        == "Verify the Relay fresh-thread app-server smoke path."
    )
    print(
        json.dumps(
            {
                "ok": ok,
                "destination_thread_id": result.destination_thread_id,
                "destination_turn_id": result.destination_turn_id,
                "cwd": thread.get("cwd"),
                "goal_objective": goal.get("objective"),
                "error": result.error,
            },
            sort_keys=True,
        )
    )
    return 0 if ok else 1


def _stop_relay_workers(repo: Path) -> None:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or "codex_app_transport.py" not in fields[1]:
            continue
        if "--worker-request" not in fields[1] or str(repo) not in fields[1]:
            continue
        try:
            os.killpg(int(fields[0]), signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            continue


def _read_thread(codex: Path, cwd: Path, thread_id: str | None) -> dict[str, object]:
    if not thread_id:
        return {}
    process = _server(codex, cwd)
    try:
        client = AppServerClient(process, response_timeout=30.0, turn_timeout=30.0)
        client.request("initialize", {"clientInfo": CLIENT_INFO})
        client.notify("initialized", {})
        result = client.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
        )
        value = result.get("thread")
        return value if isinstance(value, dict) else {}
    finally:
        _stop(process)


def _read_goal(codex: Path, cwd: Path, thread_id: str | None) -> dict[str, object]:
    if not thread_id:
        return {}
    process = _server(codex, cwd)
    try:
        client = AppServerClient(process, response_timeout=30.0, turn_timeout=30.0)
        client.request("initialize", {"clientInfo": CLIENT_INFO})
        client.notify("initialized", {})
        result = client.request("thread/goal/get", {"threadId": thread_id})
        value = result.get("goal")
        return value if isinstance(value, dict) else {}
    finally:
        _stop(process)


def _server(codex: Path, cwd: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [str(codex), "app-server", "--stdio"],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    if process.stdin is not None:
        process.stdin.close()
    if process.stdout is not None:
        process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
