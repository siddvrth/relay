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
from collections import deque
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
MAX_PROGRESS_CHARS = 2400


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
                failure = _destination_failure(state)
                if failure is None:
                    return _quiesce(event, state)
                failed_state = dict(state)
                failed_state.update(
                    {"status": "failed", "error": failure, "updated_at": _timestamp()}
                )
                _write_state(state_path, failed_state)

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
            if goal is None or goal.status not in (None, "active"):
                return _allow(event)
            handoff = _handoff_context(payload)
            settings = _execution_settings(handoff.get("turn_context"))
            if settings is None:
                return _allow(event)
            objective = _bounded(goal.objective.strip(), MAX_GOAL_CHARS)
            objective_source = "thread/goal/get"
            request = _request(payload)
            files = _changed_files(repo)
            progress = handoff.get("recent_progress")
            continuation = _continuation(
                repo=repo,
                source_session_id=session_id,
                objective=objective,
                request=request,
                files=files,
                progress=progress if isinstance(progress, str) else None,
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
                    settings=settings,
                    outcome_path=state_path.with_suffix(".outcome.json"),
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
    output = result.stdout.strip()
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
) -> str:
    lines = [
        "Relay continuation in a genuinely fresh Codex thread.",
        "Do not rely on the predecessor transcript and do not use thread/fork.",
        f"Goal objective: {objective}",
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
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "destination worker exited before reporting completion"
        except PermissionError:
            pass
    return None


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
