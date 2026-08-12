"""Launch a detached fresh Codex app-server thread and return its IDs."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from codex_app_protocol import (
    GoalSnapshot,
    ProtocolAcknowledgement,
    ProtocolConfig,
    start_protocol,
)


@dataclass(frozen=True, slots=True)
class LaunchConfig:
    cwd: Path
    continuation_prompt: str
    codex_binary: Path
    goal: GoalSnapshot | None = None
    response_timeout: float = 30.0
    turn_timeout: float = 3600.0


@dataclass(frozen=True, slots=True)
class LaunchResult:
    acknowledged: bool
    destination_thread_id: str | None
    destination_turn_id: str | None
    error: str | None = None


def launch(
    config: LaunchConfig,
    *,
    acknowledgement_timeout: float = 30.0,
) -> LaunchResult:
    """Start the worker and wait only until ``thread/start`` and ``turn/start``.

    The worker stays detached and owns the long-running continuation turn.  The
    hook process therefore returns promptly after the destination is real.
    """

    try:
        _validate(config)
    except ValueError as error:
        return LaunchResult(False, None, None, str(error))

    request_dir = config.cwd / ".omx" / "state" / "relay"
    request_dir.mkdir(parents=True, exist_ok=True)
    request_path = request_dir / f".request-{uuid.uuid4().hex}.json"
    request_path.write_text(json.dumps(_config_json(config)), encoding="utf-8")
    read_fd, write_fd = os.pipe()
    try:
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker-request",
                str(request_path),
                "--ack-fd",
                str(write_fd),
            ],
            cwd=config.cwd,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
    except OSError as error:
        os.close(read_fd)
        os.close(write_fd)
        request_path.unlink(missing_ok=True)
        return LaunchResult(False, None, None, f"worker_start_failed: {error}")

    os.close(write_fd)
    ready, _, _ = select.select([read_fd], [], [], acknowledgement_timeout)
    if not ready:
        _stop_worker(worker)
        os.close(read_fd)
        request_path.unlink(missing_ok=True)
        return LaunchResult(
            False,
            None,
            None,
            "launch_acknowledgement_timeout",
        )
    with os.fdopen(read_fd, "r", encoding="utf-8") as handle:
        line = handle.readline()

    if not line:
        return LaunchResult(False, None, None, "worker_closed_acknowledgement")
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        return LaunchResult(False, None, None, "worker_sent_invalid_acknowledgement")
    if not isinstance(message, dict):
        return LaunchResult(False, None, None, "worker_sent_invalid_acknowledgement")
    if message.get("acknowledged") is not True:
        return LaunchResult(False, None, None, str(message.get("error") or "launch_failed"))
    thread_id = message.get("thread_id")
    turn_id = message.get("turn_id")
    if not isinstance(thread_id, str) or not isinstance(turn_id, str):
        return LaunchResult(False, None, None, "worker_acknowledgement_missing_ids")
    _release_worker_handle(worker)
    return LaunchResult(True, thread_id, turn_id)


def _worker_main(request_path: Path, ack_fd: int) -> int:
    acknowledged = False
    with os.fdopen(ack_fd, "w", encoding="utf-8") as acknowledgement:
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            config = _config_from_json(payload)

            def acknowledge(result: ProtocolAcknowledgement) -> None:
                nonlocal acknowledged
                if acknowledged:
                    return
                json.dump(
                    {
                        "acknowledged": True,
                        "thread_id": result.thread_id,
                        "turn_id": result.turn_id,
                    },
                    acknowledgement,
                )
                acknowledgement.write("\n")
                acknowledgement.flush()
                acknowledged = True

            completion = start_protocol(config, on_acknowledged=acknowledge)
            if not acknowledged:
                acknowledge(completion.acknowledgement)
            return 0
        except Exception as error:  # The source hook fails open on this path.
            if not acknowledged:
                json.dump(
                    {"acknowledged": False, "error": str(error)},
                    acknowledgement,
                )
                acknowledgement.write("\n")
                acknowledgement.flush()
            return 1
        finally:
            request_path.unlink(missing_ok=True)


def _validate(config: LaunchConfig) -> None:
    if config.cwd.resolve() != config.cwd:
        raise ValueError("destination cwd must be absolute")
    if not config.cwd.is_dir():
        raise ValueError(f"destination cwd does not exist: {config.cwd}")
    if not config.continuation_prompt.strip():
        raise ValueError("continuation prompt is empty")
    if not config.codex_binary.is_file():
        raise ValueError(f"codex binary not found: {config.codex_binary}")
    if config.response_timeout <= 0 or config.turn_timeout <= 0:
        raise ValueError("app-server timeouts must be positive")


def _config_json(config: LaunchConfig) -> dict[str, object]:
    goal = config.goal
    return {
        "cwd": str(config.cwd),
        "continuation_prompt": config.continuation_prompt,
        "codex_binary": str(config.codex_binary),
        "goal_objective": goal.objective if goal else None,
        "goal_token_budget": goal.token_budget if goal else None,
        "response_timeout": config.response_timeout,
        "turn_timeout": config.turn_timeout,
    }


def _config_from_json(payload: object) -> ProtocolConfig:
    if not isinstance(payload, dict):
        raise ValueError("worker request is not an object")
    objective = payload.get("goal_objective")
    token_budget = payload.get("goal_token_budget")
    return ProtocolConfig(
        cwd=Path(str(payload["cwd"])),
        continuation_prompt=str(payload["continuation_prompt"]),
        codex_binary=Path(str(payload["codex_binary"])),
        stderr_path=Path(os.devnull),
        response_timeout=float(payload.get("response_timeout", 30.0)),
        turn_timeout=float(payload.get("turn_timeout", 3600.0)),
        goal_objective=objective if isinstance(objective, str) else None,
        goal_token_budget=(
            token_budget
            if isinstance(token_budget, int) and not isinstance(token_budget, bool)
            else None
        ),
    )


def _stop_worker(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is not None:
        return
    try:
        os.killpg(worker.pid, signal.SIGTERM)
        worker.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)


def _release_worker_handle(worker: subprocess.Popen[bytes]) -> None:
    """Let the hook process exit while the detached worker owns the turn."""

    # The worker is deliberately orphaned when this short-lived hook exits;
    # launchd/systemd reaps it after the fresh turn completes.  Marking the
    # local Popen handle as detached avoids a false ResourceWarning in Python.
    worker._child_created = False


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] != "--worker-request" or sys.argv[3] != "--ack-fd":
        return 2
    return _worker_main(Path(sys.argv[2]), int(sys.argv[4]))


if __name__ == "__main__":
    raise SystemExit(main())
