"""Automatic 30% context handoff for the Relay Codex plugin."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import context_usage
from codex_app_protocol import GoalSnapshot, read_thread_goal
from codex_app_transport import LaunchConfig, launch


DEFAULT_THRESHOLD = 0.30
MAX_GOAL_CHARS = 4000  # Codex's current thread/goal/set contract limit.
MAX_REQUEST_CHARS = 1200
MAX_FILE_HINT_CHARS = 1200


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

    if event not in {"UserPromptSubmit", "PreToolUse"}:
        return _allow(event)
    if not _valid_threshold(threshold):
        return _allow(event)

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return _allow(event)
    session_id = session_id.strip()
    repo = repo.resolve()
    state_path, lock_path = _state_paths(repo, session_id)

    try:
        with _locked(lock_path):
            state = _read_state(state_path)
            if state and state.get("status") == "running":
                return _quiesce(event, state)

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
            objective, objective_source = _objective(payload, goal)
            request = _request(payload)
            files = _changed_files(repo)
            continuation = _continuation(
                repo=repo,
                source_session_id=session_id,
                objective=objective,
                request=request,
                files=files,
                ratio=ratio,
                threshold=threshold,
            )
            _write_state(
                state_path,
                {
                    "version": 1,
                    "status": "starting",
                    "source_session_id": session_id,
                    "cwd": str(repo),
                    "objective": objective,
                    "objective_source": objective_source,
                    "next_action": "Inspect live repository state and continue the Goal.",
                    "relevant_files": files,
                    "context_used": round(ratio, 6),
                    "threshold": threshold,
                    "created_at": _timestamp(),
                },
            )

            result = launch(
                LaunchConfig(
                    cwd=repo,
                    continuation_prompt=continuation,
                    codex_binary=binary,
                    goal=goal,
                    response_timeout=_timeout("RELAY_APP_SERVER_RESPONSE_TIMEOUT", 30.0),
                    turn_timeout=_timeout("RELAY_APP_SERVER_TURN_TIMEOUT", 3600.0),
                )
            )
            if not result.acknowledged:
                _write_state(
                    state_path,
                    {
                        "version": 1,
                        "status": "failed",
                        "source_session_id": session_id,
                        "cwd": str(repo),
                        "objective": objective,
                        "objective_source": objective_source,
                        "context_used": round(ratio, 6),
                        "threshold": threshold,
                        "error": result.error or "fresh thread launch failed",
                        "updated_at": _timestamp(),
                    },
                )
                return _allow(event)

            running_state = {
                "version": 1,
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


def _objective(
    payload: Mapping[str, Any],
    goal: GoalSnapshot | None,
) -> tuple[str, str]:
    if goal is not None and goal.objective.strip():
        return _bounded(goal.objective.strip(), MAX_GOAL_CHARS), "thread/goal/get"
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return _bounded(prompt.strip(), MAX_GOAL_CHARS), "UserPromptSubmit.prompt"
    return "Continue the active task in this repository.", "fallback"


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
    output = result.stdout.strip()
    return _bounded(output or "none reported", MAX_FILE_HINT_CHARS)


def _continuation(
    *,
    repo: Path,
    source_session_id: str,
    objective: str,
    request: str | None,
    files: str,
    ratio: float,
    threshold: float,
) -> str:
    lines = [
        "Relay continuation in a genuinely fresh Codex thread.",
        "Do not rely on the predecessor transcript and do not use thread/fork.",
        f"Goal objective: {objective}",
    ]
    if request and request != objective:
        lines.append(f"Current user request: {request}")
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


def _quiesce(event: str, state: Mapping[str, Any]) -> dict[str, Any]:
    thread_id = state.get("destination_thread_id")
    suffix = f" Destination thread: {thread_id}." if thread_id else ""
    reason = "Relay started a fresh successor thread; the predecessor is quiesced." + suffix
    if event == "UserPromptSubmit":
        return {"decision": "block", "reason": reason}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow(event: str) -> dict[str, Any]:
    return {} if event == "PreToolUse" else {"continue": True}


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
    parser.add_argument("--official-hook-event", choices=("UserPromptSubmit", "PreToolUse"), required=True)
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
