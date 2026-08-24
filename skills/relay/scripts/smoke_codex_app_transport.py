#!/usr/bin/env python3
"""Prove an installed automatic A -> B -> C Relay chain with real Codex."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import context_usage
from codex_app_jsonrpc import AppServerClient
from codex_app_protocol import CLIENT_INFO


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
GOAL = (
    "Relay smoke lifecycle. If this turn has no Relay continuation context, "
    "keep the Goal active and reply RELAY_SMOKE_A_WAIT. In the first fresh "
    "successor, create relay-smoke-B.txt with the exact text B_WORK_OK using a "
    "real file or shell tool, read it back, emit RELAY_SMOKE_B_WORK_DONE, and "
    "do not complete the Goal. Keep the Goal active and repeatedly run harmless "
    "git status checks until Relay hands off. In the next successor whose "
    "continuation reports RELAY_SMOKE_B_WORK_DONE, use no workspace tools, reply "
    "RELAY_SMOKE_C_ACK, and leave the Goal active for harness-controlled completion."
)
MODEL = "gpt-5.6-luna"
EFFORT = "low"
SEED_THRESHOLD = "1.0"


def _approval_policy() -> str:
    return os.environ.get("RELAY_SMOKE_APPROVAL_POLICY", "on-request")


def main() -> int:
    codex_value = shutil.which("codex")
    auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if codex_value is None:
        print(json.dumps({"ok": False, "skipped": True, "reason": "codex not found"}))
        return 2
    if not auth.is_file():
        print(json.dumps({"ok": False, "skipped": True, "reason": "Codex auth not found"}))
        return 2

    with tempfile.TemporaryDirectory(prefix="relay-installed-smoke-") as temporary:
        root = Path(temporary).resolve()
        home = root / "codex-home"
        repo = root / "repo"
        marketplace = root / "marketplace"
        home.mkdir()
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        shutil.copy2(auth, home / "auth.json")
        codex = Path(codex_value).resolve()
        installed = _install_plugin(home, marketplace, codex)
        environment = os.environ.copy()
        for variable in (
            "OMO_CODEX_HOOKS_DISABLED",
            "CODEX_HOOKS_DISABLED",
            "CODEX_SESSION_ID",
            "CODEX_THREAD_ID",
            "CODEX_CI",
            "CODEX_SHELL",
            "RELAY_DESKTOP_HANDOFF",
        ):
            environment.pop(variable, None)
        environment.update(
            {
                "CODEX_HOME": str(home),
                "PLUGIN_ROOT": str(installed),
                "RELAY_CODEX_BINARY": str(codex),
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "",
                "RELAY_PRESENTATION_MODE": "headless",
                "RELAY_THRESHOLD": SEED_THRESHOLD,
                "RELAY_APP_SERVER_RESPONSE_TIMEOUT": "30",
                "RELAY_APP_SERVER_TURN_TIMEOUT": "300",
                "ROOT": str(repo),
            }
        )
        _trust_installed_relay_hooks(codex, environment, home, repo)
        try:
            try:
                result = _run_chain(codex, environment, home, repo, installed)
            except Exception:
                failure_dir = os.environ.get("RELAY_SMOKE_FAILURE_DIR")
                if failure_dir:
                    destination = Path(failure_dir).expanduser().resolve()
                    if destination.exists():
                        raise RuntimeError(
                            f"smoke failure directory already exists: {destination}"
                        )
                    shutil.copytree(root, destination)
                    print(
                        f"real smoke artifacts preserved at {destination}",
                        file=sys.stderr,
                    )
                raise
        finally:
            try:
                from relay import cleanup_workers

                cleanup_workers(repo)
            except (ImportError, OSError, ValueError, TypeError):
                pass
            _stop_relay_workers(repo)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") is True else 1


def _run_chain(
    codex: Path,
    environment: dict[str, str],
    home: Path,
    repo: Path,
    installed: Path,
) -> dict[str, object]:
    seed_process = _server(codex, repo, environment)
    seed_client = _client(seed_process)
    try:
        source_settings = _settings(repo)
        source = seed_client.request(
            "thread/start",
            {
                "cwd": str(repo),
                "model": MODEL,
                "approvalPolicy": _approval_policy(),
                "approvalsReviewer": "auto_review",
                "sandbox": "workspace-write",
                "personality": "pragmatic",
                "serviceName": "relay-smoke",
            },
        )["thread"]["id"]
        _turn(
            seed_client,
            source,
            "This is source A. Reply exactly RELAY_SMOKE_A_READY and do not modify files yet.",
            source_settings,
        )
        seed_client.request(
            "thread/goal/set",
            {"threadId": source, "objective": GOAL, "status": "active"},
        )

        source_path = _thread_path(seed_client, home, source)
        initial_ratio = context_usage.extract_context_used(
            {"transcript_path": str(source_path)}
        )
    finally:
        _stop(seed_process)

    threshold = min((initial_ratio or 0.0) + 0.20, 0.90)
    trigger_environment = dict(environment)
    trigger_environment["RELAY_THRESHOLD"] = f"{threshold:.6f}"
    _cross_threshold(source_path, threshold)
    process: subprocess.Popen[bytes] | None = None
    try:
        _invoke_installed_hook(
            installed,
            repo,
            trigger_environment,
            source,
            source_path,
            "Continue the active Relay smoke Goal now.",
        )
        b = _destination(repo, source)
        a_state = _wait_state(repo, source)
        b_path = _wait_thread_path(home, b)
        marker = repo / "relay-smoke-B.txt"
        marker_ok = _wait_marker(marker) == "B_WORK_OK"
        _wait_agent_message(b_path, "RELAY_SMOKE_B_WORK_DONE", timeout=90)
        b_goal_before = _wait_goal_status_isolated(
            codex,
            repo,
            trigger_environment,
            b,
            "active",
        )
        b_settings = _first_settings_context(b_path)

        _cross_threshold(b_path, threshold)
        _invoke_installed_hook(
            installed,
            repo,
            trigger_environment,
            b,
            b_path,
            "Continue the active Relay smoke Goal and hand off now.",
        )
        c = _destination(repo, b)
        b_state = _wait_state(repo, b)
        a_outcome = _wait_outcome(repo, source, "completed")
        b_goal_after = _wait_goal_status_isolated(
            codex,
            repo,
            trigger_environment,
            b,
            "active",
        )
        _require_worker_cleanup(a_state, label="A-to-B")

        c_path = _wait_thread_path(home, c)
        _wait_agent_message(c_path, "RELAY_SMOKE_C_ACK", timeout=90)
        process = _server(codex, repo, trigger_environment)
        client = _client(process)
        c_goal_before_control = _wait_goal_status(client, c, "active")
        completed_goal = client.request(
            "thread/goal/set",
            {"threadId": c, "status": "complete"},
        ).get("goal")
        if (
            not isinstance(completed_goal, dict)
            or completed_goal.get("status") != "complete"
        ):
            raise RuntimeError("thread/goal/set did not complete C's Goal")
        c_goal = _wait_goal_status(client, c, "complete")
        b_outcome = _wait_outcome(repo, b, "completed")
        _require_worker_cleanup(b_state, label="B-to-C")
        c_thread = _read_thread(client, c)
        _stop(process)
        c_settings = _first_settings_context(c_path)
        thread_ids_before_retry_check = _session_thread_ids(home, repo)

        time.sleep(0.5)
        thread_ids = _session_thread_ids(home, repo)

        expected = _settings_fingerprint(_first_settings_context(source_path))
        b_fingerprint = _settings_fingerprint(b_settings)
        c_fingerprint = _settings_fingerprint(c_settings)
        b_text = b_path.read_text(encoding="utf-8")
        c_text = json.dumps(c_thread)
        b_stayed_active = (
            b_goal_before.get("status") == "active"
            and b_goal_after.get("status") == "active"
        )
        a_did_not_retry = bool(
            a_outcome.get("status") == "completed"
            and a_state.get("destination_thread_id") == b
            and thread_ids_before_retry_check == thread_ids == {source, b, c}
        )
        ok = bool(
            source != b
            and b != c
            and source != c
            and marker_ok
            and "RELAY_SMOKE_B_WORK_DONE" in b_text
            and "RELAY_SMOKE_C_ACK" in c_text
            and b_goal_before.get("objective") == GOAL
            and b_goal_after.get("objective") == GOAL
            and c_goal.get("objective") == GOAL
            and c_goal_before_control.get("status") == "active"
            and c_goal.get("status") == "complete"
            and b_stayed_active
            and a_did_not_retry
            and b_outcome.get("status") == "completed"
            and expected == b_fingerprint == c_fingerprint
            and thread_ids == {source, b, c}
        )
        result: dict[str, object] = {
            "ok": ok,
            "source_thread_id": source,
            "first_destination_thread_id": b,
            "second_destination_thread_id": c,
            "first_destination_name": a_state.get("destination_thread_name"),
            "second_destination_name": b_state.get("destination_thread_name"),
            "marker_ok": marker_ok,
            "goal_preserved": (
                b_goal_before.get("objective")
                == b_goal_after.get("objective")
                == c_goal.get("objective")
                == GOAL
            ),
            "b_goal_stayed_active": b_stayed_active,
            "c_goal_active_before_control": (
                c_goal_before_control.get("status") == "active"
            ),
            "c_goal_completed": c_goal.get("status") == "complete",
            "a_did_not_retry": a_did_not_retry,
            "a_worker_outcome": a_outcome.get("status"),
            "b_worker_outcome": b_outcome.get("status"),
            "worker_cleanup": True,
            "settings_preserved": expected == b_fingerprint == c_fingerprint,
            "thread_count": len(thread_ids),
            "no_duplicates": thread_ids == {source, b, c},
            "b_ack": "RELAY_SMOKE_B_WORK_DONE" in b_text,
            "c_ack": "RELAY_SMOKE_C_ACK" in c_text,
            "test_threshold": round(threshold, 6),
        }
        if not ok:
            result["source_settings"] = expected
            result["first_destination_settings"] = b_fingerprint
            result["second_destination_settings"] = c_fingerprint
        return result
    finally:
        if process is not None:
            _stop(process)


def _settings(repo: Path) -> dict[str, object]:
    return {
        "cwd": str(repo),
        "model": MODEL,
        "effort": EFFORT,
        "personality": "pragmatic",
        "approvalPolicy": _approval_policy(),
        "approvalsReviewer": "auto_review",
        "sandboxPolicy": {
            "type": "workspaceWrite",
            "writableRoots": [str(repo)],
            "networkAccess": False,
        },
        "collaborationMode": {
            "mode": "default",
            "settings": {
                "model": MODEL,
                "reasoning_effort": EFFORT,
                "developer_instructions": None,
            },
        },
        "summary": "concise",
    }


def _turn(
    client: AppServerClient,
    thread_id: str,
    prompt: str,
    settings: dict[str, object],
) -> str:
    params: dict[str, object] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
        **settings,
    }
    result = client.request("turn/start", params)
    turn = result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise RuntimeError("turn/start returned no turn id")
    turn_id = turn["id"]
    client.wait_for_completion(turn_id)
    return turn_id


def _invoke_installed_hook(
    installed: Path,
    repo: Path,
    environment: dict[str, str],
    thread_id: str,
    transcript_path: Path,
    prompt: str,
) -> None:
    payload = {
        "session_id": thread_id,
        "cwd": str(repo),
        "prompt": prompt,
        "transcript_path": str(transcript_path),
    }
    hook_environment = dict(environment)
    hook_environment.update(
        {
            "PLUGIN_ROOT": str(installed),
            "ROOT": str(repo),
        }
    )
    try:
        result = subprocess.run(
            ["bash", str(installed / "hooks" / "relay_hook.sh"), "UserPromptSubmit"],
            cwd=repo,
            env=hook_environment,
            input=json.dumps(payload, separators=(",", ":")),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"installed Relay hook failed to run: {error}") from error
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"installed Relay hook returned invalid JSON: {result.stdout!r}"
        ) from error
    if (
        result.returncode != 0
        or not isinstance(response, dict)
        or response.get("decision") != "block"
        or response.get("continue") is not False
    ):
        raise RuntimeError(
            "installed Relay hook did not acknowledge handoff: "
            f"returncode={result.returncode}; response={response!r}; "
            f"stderr={result.stderr!r}"
        )
    _wait_state(repo, thread_id)


def _destination(repo: Path, source: str) -> str:
    state = _wait_state(repo, source)
    destination = state.get("destination_thread_id")
    if not isinstance(destination, str) or not destination:
        raise RuntimeError(f"Relay state for {source} has no destination")
    return destination


def _wait_state(repo: Path, source: str) -> dict[str, object]:
    path = _state_path(repo, source)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if isinstance(value, dict):
            status = value.get("status")
            if status == "running":
                return value
            if status in {"failed", "circuit_breaker"}:
                raise RuntimeError(
                    f"Relay state for {source} stopped with {status}: "
                    f"{value.get('error') or 'no error recorded'}"
                )
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for Relay state for {source}")


def _wait_outcome(
    repo: Path,
    source: str,
    status: str,
    *,
    timeout: float = 15.0,
) -> dict[str, object]:
    path = _state_path(repo, source).with_suffix(".outcome.json")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if isinstance(value, dict) and value.get("status") == status:
            return value
        if isinstance(value, dict) and value.get("status") == "failed":
            raise RuntimeError(str(value.get("error") or "Relay destination failed"))
        time.sleep(0.1)
    state_path = _state_path(repo, source)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = None
    raise RuntimeError(
        f"timed out waiting for {source} outcome {status}; state={state!r}"
    )


def _require_worker_cleanup(
    state: dict[str, object],
    *,
    label: str,
    timeout: float = 10.0,
) -> None:
    worker_pid = state.get("worker_pid")
    if not isinstance(worker_pid, int) or isinstance(worker_pid, bool):
        raise RuntimeError(f"{label} state has no worker pid: {state!r}")
    deadline = time.monotonic() + timeout
    members = _live_process_group_members(worker_pid)
    while members and time.monotonic() < deadline:
        time.sleep(0.05)
        members = _live_process_group_members(worker_pid)
    if members:
        raise RuntimeError(
            f"{label} worker did not exit promptly; "
            f"live_process_group={members!r}; state={state!r}"
        )


def _live_process_group_members(pgid: int) -> list[str]:
    result = subprocess.run(
        ["ps", "-axo", "pgid=,stat=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    members: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            candidate = int(fields[0])
        except ValueError:
            continue
        if candidate == pgid and not fields[1].startswith("Z"):
            members.append(fields[2])
    return members


def _state_path(repo: Path, source: str) -> Path:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]
    return repo / ".omx" / "state" / "relay" / f"{digest}.json"


def _cross_threshold(path: Path, threshold: float) -> None:
    ratio = min(threshold + 0.05, 0.99)
    total_tokens = round(12_000 + ratio * (100_000 - 12_000))
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"total_tokens": total_tokens},
                "model_context_window": 100_000,
            },
        },
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _first_settings_context(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "turn_context":
                payload = record.get("payload")
                if isinstance(payload, dict) and isinstance(payload.get("model"), str):
                    return payload
    return {}


def _settings_fingerprint(value: dict[str, object]) -> dict[str, object]:
    sandbox = value.get("sandbox_policy")
    return {
        "model": value.get("model"),
        "personality": value.get("personality"),
        "approval_policy": value.get("approval_policy"),
        "approvals_reviewer": value.get("approvals_reviewer"),
        "sandbox": {
            "type": sandbox.get("type"),
            "network_access": sandbox.get("network_access"),
        }
        if isinstance(sandbox, dict)
        else None,
    }


def _read_thread(client: AppServerClient, thread_id: str) -> dict[str, object]:
    result = client.request(
        "thread/read",
        {"threadId": thread_id, "includeTurns": True},
    )
    value = result.get("thread")
    return value if isinstance(value, dict) else {}


def _goal(client: AppServerClient, thread_id: str) -> dict[str, object]:
    value = client.request("thread/goal/get", {"threadId": thread_id}).get("goal")
    return value if isinstance(value, dict) else {}


def _wait_goal_status(
    client: AppServerClient,
    thread_id: str,
    status: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        goal = _goal(client, thread_id)
        if goal.get("status") == status:
            return goal
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {thread_id} Goal status {status}")


def _wait_goal_status_isolated(
    codex: Path,
    repo: Path,
    environment: dict[str, str],
    thread_id: str,
    status: str,
) -> dict[str, object]:
    process = _server(codex, repo, environment)
    client = _client(process)
    try:
        return _wait_goal_status(client, thread_id, status)
    finally:
        _stop(process)


def _thread_path(client: AppServerClient, home: Path, thread_id: str) -> Path:
    value = _read_thread(client, thread_id).get("path")
    if isinstance(value, str) and Path(value).is_file():
        return Path(value)
    matches = list((home / "sessions").rglob(f"*{thread_id}.jsonl"))
    if len(matches) != 1:
        raise RuntimeError(f"could not find rollout for {thread_id}")
    return matches[0]


def _wait_thread_path(home: Path, thread_id: str) -> Path:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        matches = list((home / "sessions").rglob(f"*{thread_id}.jsonl"))
        if len(matches) == 1:
            return matches[0]
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for rollout {thread_id}")


def _wait_marker(path: Path, *, timeout: float = 120.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            time.sleep(0.05)
            continue
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError("timed out waiting for real B workspace action")


def _wait_agent_message(path: Path, text: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload") if isinstance(record, dict) else None
                    if (
                        isinstance(record, dict)
                        and record.get("type") == "event_msg"
                        and isinstance(payload, dict)
                        and payload.get("type") == "task_complete"
                        and isinstance(payload.get("last_agent_message"), str)
                        and text in payload["last_agent_message"]
                    ):
                        return
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for completed assistant message {text}")


def _session_thread_ids(home: Path, repo: Path) -> set[str]:
    thread_ids: set[str] = set()
    for path in (home / "sessions").rglob("*.jsonl"):
        try:
            first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        except (OSError, IndexError, json.JSONDecodeError):
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("cwd") != str(repo)
            or payload.get("parent_thread_id") is not None
        ):
            continue
        identifier = payload.get("id")
        if isinstance(identifier, str):
            thread_ids.add(identifier)
    return thread_ids


def _install_plugin(home: Path, marketplace: Path, codex: Path) -> Path:
    package = marketplace / "plugins" / "relay"
    (marketplace / ".agents" / "plugins").mkdir(parents=True)
    package.mkdir(parents=True)
    shutil.copytree(PLUGIN_ROOT / ".codex-plugin", package / ".codex-plugin")
    shutil.copytree(PLUGIN_ROOT / "hooks", package / "hooks")
    shutil.copytree(PLUGIN_ROOT / "skills", package / "skills")
    manifest = {
        "name": "relay-smoke",
        "plugins": [
            {"name": "relay", "source": {"source": "local", "path": "./plugins/relay"}}
        ],
    }
    (marketplace / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    subprocess.run(
        [str(codex), "plugin", "marketplace", "add", str(marketplace), "--json"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    installed = subprocess.run(
        [str(codex), "plugin", "add", "relay@relay-smoke", "--json"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(installed.stdout).get("installedPath")
    if not isinstance(value, str):
        raise RuntimeError("plugin install returned no installedPath")
    return Path(value).resolve()


def _trust_installed_relay_hooks(
    codex: Path,
    environment: dict[str, str],
    home: Path,
    repo: Path,
) -> None:
    hooks = _list_hooks(codex, environment, repo)
    relay_hooks = [
        item
        for item in hooks
        if item.get("source") == "plugin"
        and item.get("pluginId") == "relay@relay-smoke"
    ]
    if len(relay_hooks) != 3:
        raise RuntimeError(f"expected three installed Relay hooks, got {relay_hooks!r}")

    blocks: list[str] = []
    for hook in relay_hooks:
        key = hook.get("key")
        current_hash = hook.get("currentHash")
        if not isinstance(key, str) or not isinstance(current_hash, str):
            raise RuntimeError(f"Relay hook has no trust identity: {hook!r}")
        blocks.extend(
            [
                f"[hooks.state.{json.dumps(key)}]",
                f"trusted_hash = {json.dumps(current_hash)}",
                "",
            ]
        )

    config = home / "config.toml"
    existing = config.read_text(encoding="utf-8") if config.exists() else ""
    prefix = "" if "[hooks.state]" in existing else "[hooks.state]\n\n"
    with config.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(prefix + "\n".join(blocks))

    trusted = _list_hooks(codex, environment, repo)
    relay_trust = {
        item.get("key"): item.get("trustStatus")
        for item in trusted
        if item.get("source") == "plugin"
        and item.get("pluginId") == "relay@relay-smoke"
    }
    if len(relay_trust) != len(relay_hooks) or set(relay_trust.values()) != {"trusted"}:
        raise RuntimeError(f"installed Relay hooks are not trusted: {relay_trust!r}")


def _list_hooks(
    codex: Path,
    environment: dict[str, str],
    repo: Path,
) -> list[dict[str, object]]:
    process = _server(codex, repo, environment)
    client = _client(process)
    try:
        result = client.request("hooks/list", {"cwds": [str(repo)]})
    finally:
        _stop(process)
    entries = result.get("data")
    if not isinstance(entries, list):
        raise RuntimeError(f"hooks/list returned an unexpected result: {result!r}")
    hooks: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            raise RuntimeError(f"hooks/list returned an unexpected entry: {entry!r}")
        for item in entry["hooks"]:
            if not isinstance(item, dict):
                raise RuntimeError(f"hooks/list returned invalid hook metadata: {item!r}")
            hooks.append(item)
    return hooks


def _server(
    codex: Path,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [str(codex), "app-server", "--stdio"],
        cwd=cwd,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )


def _client(process: subprocess.Popen[bytes]) -> AppServerClient:
    client = AppServerClient(process, response_timeout=30.0, turn_timeout=300.0)
    client.request(
        "initialize",
        {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}},
    )
    client.notify("initialized", {})
    return client


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=5)
    if process.stdin is not None:
        process.stdin.close()
    if process.stdout is not None:
        process.stdout.close()


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
            os.kill(int(fields[0]), 15)
        except (ProcessLookupError, ValueError, PermissionError):
            continue


if __name__ == "__main__":
    raise SystemExit(main())
