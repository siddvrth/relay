"""Launch a detached fresh Codex app-server thread and return its IDs."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
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
    settings: dict[str, object] | None = None
    outcome_path: Path | None = None
    response_timeout: float = 30.0
    turn_timeout: float = 3600.0
    thread_name: str | None = None
    source_thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class LaunchResult:
    acknowledged: bool
    destination_thread_id: str | None
    destination_turn_id: str | None
    worker_pid: int | None = None
    error: str | None = None


def launch(
    config: LaunchConfig,
    *,
    acknowledgement_timeout: float = 30.0,
) -> LaunchResult:
    """Start the worker and wait until the destination is safely acknowledged.

    The worker stays detached while the destination owns the Goal, then closes
    its app-server and writes a completed outcome when that Goal becomes
    terminal or the destination acknowledges its own successor. The hook
    process returns promptly after the destination is real and its first turn
    has started.
    """

    try:
        _validate(config)
    except ValueError as error:
        return LaunchResult(False, None, None, error=str(error))

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
        return LaunchResult(False, None, None, error=f"worker_start_failed: {error}")

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
            error="launch_acknowledgement_timeout",
        )
    with os.fdopen(read_fd, "r", encoding="utf-8") as handle:
        line = handle.readline()

    if not line:
        _reap_worker(worker)
        return LaunchResult(False, None, None, error="worker_closed_acknowledgement")
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        _reap_worker(worker)
        return LaunchResult(False, None, None, error="worker_sent_invalid_acknowledgement")
    if not isinstance(message, dict):
        _reap_worker(worker)
        return LaunchResult(False, None, None, error="worker_sent_invalid_acknowledgement")
    if message.get("acknowledged") is not True:
        _reap_worker(worker)
        return LaunchResult(
            False,
            None,
            None,
            error=str(message.get("error") or "launch_failed"),
        )
    thread_id = message.get("thread_id")
    turn_id = message.get("turn_id")
    if not isinstance(thread_id, str) or not isinstance(turn_id, str):
        _reap_worker(worker)
        return LaunchResult(False, None, None, error="worker_acknowledgement_missing_ids")
    _release_worker_handle(worker)
    return LaunchResult(
        True,
        thread_id,
        turn_id,
        worker_pid=worker.pid,
    )


def _worker_main(request_path: Path, ack_fd: int) -> int:
    acknowledged = False
    acknowledgement_result: ProtocolAcknowledgement | None = None
    outcome_path: Path | None = None
    with os.fdopen(ack_fd, "w", encoding="utf-8") as acknowledgement:
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("outcome_path"), str):
                outcome_path = Path(payload["outcome_path"])
                _write_outcome(
                    outcome_path,
                    {
                        "status": "running",
                        "worker_pid": os.getpid(),
                        "worker_pgid": os.getpid(),
                        "cwd": payload.get("cwd") if isinstance(payload, dict) else None,
                        "codex_binary": payload.get("codex_binary") if isinstance(payload, dict) else None,
                        "request_path": str(request_path),
                    },
                )
            config = _config_from_json(payload)

            def acknowledge(result: ProtocolAcknowledgement) -> None:
                nonlocal acknowledged, acknowledgement_result
                if acknowledged:
                    return
                acknowledgement_result = result
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
            if not completion.destination_readable:
                raise RuntimeError("destination thread was not readable after completion")
            if outcome_path is not None:
                _write_outcome(
                    outcome_path,
                    {
                        "status": "completed",
                        "worker_pid": os.getpid(),
                        "thread_id": completion.acknowledgement.thread_id,
                        "turn_id": completion.acknowledgement.turn_id,
                    },
                )
            return 0
        except Exception as error:  # The source hook fails open on this path.
            if outcome_path is not None:
                failed: dict[str, object] = {
                    "status": "failed",
                    "error": str(error),
                }
                if acknowledgement_result is not None:
                    failed["thread_id"] = acknowledgement_result.thread_id
                    failed["turn_id"] = acknowledgement_result.turn_id
                _write_outcome(outcome_path, failed)
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
    if config.outcome_path is not None and not config.outcome_path.is_absolute():
        raise ValueError("outcome path must be absolute")


def _config_json(config: LaunchConfig) -> dict[str, object]:
    goal = config.goal
    return {
        "cwd": str(config.cwd),
        "continuation_prompt": config.continuation_prompt,
        "codex_binary": str(config.codex_binary),
        "goal_objective": goal.objective if goal else None,
        "goal_status": goal.status if goal else None,
        "goal_token_budget": goal.token_budget if goal else None,
        "settings": config.settings,
        "outcome_path": str(config.outcome_path) if config.outcome_path else None,
        "response_timeout": config.response_timeout,
        "turn_timeout": config.turn_timeout,
        "thread_name": config.thread_name,
        "source_thread_id": config.source_thread_id,
    }


def _config_from_json(payload: object) -> ProtocolConfig:
    if not isinstance(payload, dict):
        raise ValueError("worker request is not an object")
    objective = payload.get("goal_objective")
    status = payload.get("goal_status")
    token_budget = payload.get("goal_token_budget")
    settings = payload.get("settings")
    return ProtocolConfig(
        cwd=Path(str(payload["cwd"])),
        continuation_prompt=str(payload["continuation_prompt"]),
        codex_binary=Path(str(payload["codex_binary"])),
        stderr_path=Path(os.devnull),
        response_timeout=float(payload.get("response_timeout", 30.0)),
        turn_timeout=float(payload.get("turn_timeout", 3600.0)),
        goal_objective=objective if isinstance(objective, str) else None,
        goal_status=status if isinstance(status, str) else None,
        goal_token_budget=(
            token_budget
            if isinstance(token_budget, int) and not isinstance(token_budget, bool)
            else None
        ),
        settings=settings if isinstance(settings, dict) else None,
        thread_name=(
            payload.get("thread_name")
            if isinstance(payload.get("thread_name"), str)
            else None
        ),
        source_thread_id=(
            payload.get("source_thread_id")
            if isinstance(payload.get("source_thread_id"), str)
            else None
        ),
    )


def _write_outcome(path: Path, payload: dict[str, object]) -> None:
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


def stop_worker_pid(
    pid: int,
    *,
    repo: Path | None = None,
    timeout: float = 5.0,
) -> bool:
    """Terminate one Relay worker process group after validating its command."""

    if pid <= 0 or not _is_relay_worker(pid, repo):
        return False
    return _terminate_process_group(pid, timeout=timeout)


def stop_worker_group(
    pid: int,
    *,
    repo: Path,
    outcome_path: Path | None = None,
    timeout: float = 5.0,
) -> bool:
    """Terminate an orphaned Relay process group after validating its outcome."""

    if pid <= 0:
        return False
    if not _is_relay_worker(pid, repo) and not _is_relay_worker_group(
        pid,
        repo,
        outcome_path,
    ):
        return False
    return _terminate_process_group(pid, timeout=timeout)


def _terminate_process_group(pid: int, *, timeout: float) -> bool:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_group_exists(pid):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return not _process_group_exists(pid)


def worker_pid_is_relay(pid: int, *, repo: Path) -> bool:
    return pid > 0 and _is_relay_worker(pid, repo)


def _is_relay_worker(pid: int, repo: Path | None) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    command = result.stdout.strip()
    if "codex_app_transport.py" not in command or "--worker-request" not in command:
        return False
    if repo is None:
        return True
    return any(candidate in command for candidate in {str(repo), str(repo.resolve())})


def _is_relay_worker_group(
    pid: int,
    repo: Path,
    outcome_path: Path | None,
) -> bool:
    if outcome_path is None or not outcome_path.is_absolute():
        return False
    try:
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(outcome, dict):
        return False
    if (
        outcome.get("worker_pid") != pid
        or outcome.get("worker_pgid") != pid
        or outcome.get("cwd") not in {str(repo), str(repo.resolve())}
    ):
        return False
    request_path = outcome.get("request_path")
    codex_binary = outcome.get("codex_binary")
    if (
        not isinstance(request_path, str)
        or not Path(request_path).is_absolute()
        or not Path(request_path).is_file()
        or not isinstance(codex_binary, str)
        or not codex_binary
    ):
        return False
    for member_pid, member_pgid, stat, command in _process_group_entries(pid):
        if (
            member_pgid == pid
            and member_pid != pid
            and not stat.startswith("Z")
            and codex_binary in command
            and ("app-server" in command or "--stdio" in command)
        ):
            return True
    return False


def _process_group_entries(pgid: int) -> list[tuple[int, int, str, str]]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    entries: list[tuple[int, int, str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) != 4:
            continue
        try:
            member_pid = int(fields[0])
            member_pgid = int(fields[1])
        except ValueError:
            continue
        if member_pgid == pgid:
            entries.append((member_pid, member_pgid, fields[2], fields[3]))
    return entries


def _process_group_exists(pgid: int) -> bool:
    return any(not stat.startswith("Z") for _, _, stat, _ in _process_group_entries(pgid))


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # A detached worker may be a zombie until its parent/launchd reaps it;
    # the process group is already dead and it cannot keep an app-server alive.
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return True
    return not result.stdout.strip().startswith("Z")


def _release_worker_handle(worker: subprocess.Popen[bytes]) -> None:
    """Let the hook exit while the worker supervises the destination."""

    # The worker is deliberately orphaned when this short-lived hook exits;
    # launchd/systemd reaps it after terminal/successor cleanup. Marking the
    # local Popen handle as detached avoids a false ResourceWarning in Python.
    setattr(worker, "_child_created", False)


def _reap_worker(worker: subprocess.Popen[bytes]) -> None:
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _stop_worker(worker)


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] != "--worker-request" or sys.argv[3] != "--ack-fd":
        return 2
    return _worker_main(Path(sys.argv[2]), int(sys.argv[4]))


if __name__ == "__main__":
    raise SystemExit(main())
