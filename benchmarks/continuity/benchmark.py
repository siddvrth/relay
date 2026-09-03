#!/usr/bin/env python3
"""Run the six-pair Relay/native Goal continuity benchmark.

This is deliberately a benchmark runner, not a Relay runtime change.  The
runner launches Codex app-server directly, gives both arms the same fixture
snapshot and Goal prompt, and keeps the hidden grader outside the task
workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shlex
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOT = ROOT / "skills" / "relay" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from codex_app_jsonrpc import AppServerClient, AppServerFailure  # noqa: E402

from fixtures import FIXTURES, PILOT_FIXTURE, Fixture, run_hidden_grade  # noqa: E402


PACKAGE_REF = os.environ.get("RELAY_BENCHMARK_PACKAGE_REF", "v0.6.0")
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_EFFORT = "medium"
DEFAULT_THRESHOLD = 40_000
DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_TIMEOUT = 1_200.0
MAX_RUNS = 12


class BenchmarkInfrastructureError(RuntimeError):
    """An error in setup/transport, distinct from an agent task failure."""


class BenchmarkPreflightError(BenchmarkInfrastructureError):
    """The resolved Codex configuration could not be proven before a task."""

    def __init__(self, message: str, evidence: dict[str, object]) -> None:
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True)
class BenchmarkConfig:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    model_auto_compact_token_limit: int = DEFAULT_THRESHOLD
    model_context_window: int = DEFAULT_CONTEXT_WINDOW
    model_auto_compact_token_limit_scope: str = "body_after_prefix"
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    network_access: bool = False
    personality: str = "pragmatic"
    timeout_seconds: float = DEFAULT_TIMEOUT
    poll_seconds: float = 1.0
    seed: int = 20260828


@dataclass
class RunningServer:
    process: subprocess.Popen[bytes]
    client: AppServerClient
    stderr_handle: Any


def _now_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _copy_auth(home: Path) -> None:
    source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    source = source_home / "auth.json"
    if not source.is_file():
        raise BenchmarkInfrastructureError(f"Codex auth not found at {source}")
    home.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, home / "auth.json")
    os.chmod(home / "auth.json", 0o600)


def _base_environment(home: Path, run_root: Path, repo: Path) -> dict[str, str]:
    os_home = run_root / "os-home"
    tmp = run_root / "tmp"
    os_home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    environment = {
        key: os.environ[key]
        for key in ("PATH", "USER", "LANG", "LC_ALL", "TERM")
        if key in os.environ
    }
    environment.update(
        {
            "CODEX_HOME": str(home),
            "HOME": str(os_home),
            "TMPDIR": str(tmp),
            "ROOT": str(repo),
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
            "RELAY_CODEX_BINARY": str(Path(shutil.which("codex") or "codex").resolve()),
            "RELAY_APP_SERVER_RESPONSE_TIMEOUT": "45",
            "RELAY_APP_SERVER_TURN_TIMEOUT": "1200",
        }
    )
    return environment


def _write_config(home: Path, config: BenchmarkConfig, *, relay: bool) -> None:
    config_text = textwrap.dedent(
        f"""
        model = {json.dumps(config.model)}
        model_reasoning_effort = {json.dumps(config.effort)}
        model_context_window = {config.model_context_window}
        model_auto_compact_token_limit = {config.model_auto_compact_token_limit}
        model_auto_compact_token_limit_scope = {json.dumps(config.model_auto_compact_token_limit_scope)}
        approval_policy = {json.dumps(config.approval_policy)}
        sandbox_mode = {json.dumps(config.sandbox)}
        network_access = {json.dumps("enabled" if config.network_access else "disabled")}
        personality = {json.dumps(config.personality)}
        service_tier = "default"

        [features]
        plugins = {str(relay).lower()}
        plugin_hooks = {str(relay).lower()}
        """
    ).lstrip()
    (home / "config.toml").write_text(config_text, encoding="utf-8")


def _start_server(
    codex: Path,
    repo: Path,
    environment: dict[str, str],
    stderr_path: Path,
    *,
    config: BenchmarkConfig,
) -> RunningServer:
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_handle = stderr_path.open("ab")
    try:
        process = subprocess.Popen(
            [str(codex), "app-server", "--stdio"],
            cwd=repo,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
            bufsize=0,
        )
    except OSError:
        stderr_handle.close()
        raise
    client = AppServerClient(
        process,
        response_timeout=45.0,
        turn_timeout=config.timeout_seconds,
    )
    try:
        client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "continuity-benchmark",
                    "title": "Continuity Benchmark",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        client.notify("initialized", {})
    except Exception:
        _stop_server(RunningServer(process, client, stderr_handle))
        raise
    return RunningServer(process, client, stderr_handle)


def _stop_server(server: RunningServer | None) -> None:
    if server is None:
        return
    process = server.process
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            pass
    try:
        server.stderr_handle.close()
    except OSError:
        pass


def _resolved_config_preflight(
    codex: Path,
    repo: Path,
    environment: dict[str, str],
    run_root: Path,
    config: BenchmarkConfig,
) -> dict[str, object]:
    """Prove effective Codex settings through app-server before task tokens."""

    server: RunningServer | None = None
    resolved: dict[str, object] = {}
    evidence: dict[str, object] = {
        "source": "app-server config/read",
        "resolved": resolved,
        "origins": {},
        "checks": {},
        "passed": False,
    }
    try:
        server = _start_server(
            codex,
            repo,
            environment,
            run_root / "preflight.stderr.log",
            config=config,
        )
        response = server.client.request("config/read", {})
        resolved_config = response.get("config")
        origins = response.get("origins")
        if not isinstance(resolved_config, dict):
            raise BenchmarkPreflightError(
                "config/read returned no resolved config object",
                evidence,
            )
        for key in (
            "model_auto_compact_token_limit",
            "model_auto_compact_token_limit_scope",
            "model_context_window",
            "model",
            "model_reasoning_effort",
        ):
            resolved[key] = resolved_config.get(key)
        if isinstance(origins, dict):
            evidence["origins"] = {
                key: origins.get(key)
                for key in resolved
                if key in origins
            }
        expected = {
            "model_auto_compact_token_limit": config.model_auto_compact_token_limit,
            "model_auto_compact_token_limit_scope": config.model_auto_compact_token_limit_scope,
            "model_context_window": config.model_context_window,
            "model": config.model,
            "model_reasoning_effort": config.effort,
        }
        checks = {
            key: resolved.get(key) == value
            for key, value in expected.items()
        }
        evidence["checks"] = checks
        evidence["passed"] = all(checks.values())
        if not evidence["passed"]:
            raise BenchmarkPreflightError(
                "resolved Codex configuration did not match the frozen benchmark settings",
                evidence,
            )
        return evidence
    except BenchmarkPreflightError:
        raise
    except (AppServerFailure, OSError, subprocess.SubprocessError, ValueError) as error:
        evidence["error"] = f"{type(error).__name__}: {error}"
        raise BenchmarkPreflightError(
            "resolved Codex configuration could not be proven by config/read",
            evidence,
        ) from error
    finally:
        _stop_server(server)


def _sandbox_policy(repo: Path, config: BenchmarkConfig) -> dict[str, object]:
    return {
        "type": "workspaceWrite",
        "writableRoots": [str(repo)],
        "networkAccess": config.network_access,
    }


def _turn_settings(repo: Path, config: BenchmarkConfig) -> dict[str, object]:
    return {
        "approvalPolicy": config.approval_policy,
        "model": config.model,
        "effort": config.effort,
        "personality": config.personality,
        "sandboxPolicy": _sandbox_policy(repo, config),
        "summary": "concise",
        "collaborationMode": {
            "mode": "default",
            "settings": {
                "model": config.model,
                "reasoning_effort": config.effort,
                "developer_instructions": None,
            },
        },
    }


def _seed_cli_thread(
    codex: Path,
    repo: Path,
    environment: dict[str, str],
    config: BenchmarkConfig,
) -> str:
    result = subprocess.run(
        [
            str(codex),
            "exec",
            "--json",
            "--cd",
            str(repo),
            "--thread-source",
            "cli",
            "--model",
            config.model,
            "--sandbox",
            config.sandbox,
            "-c",
            f'approval_policy={json.dumps(config.approval_policy)}',
            "-c",
            f'model_reasoning_effort={json.dumps(config.effort)}',
            "-c",
            f'network_access={json.dumps("enabled" if config.network_access else "disabled")}',
            "Initialize this headless benchmark workspace. Do not inspect or modify files; reply exactly CONTINUITY_SEED_READY.",
        ],
        cwd=repo,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=config.timeout_seconds,
        check=False,
    )
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        candidate = value.get("thread_id") or value.get("threadId")
        if not isinstance(candidate, str):
            thread = value.get("thread")
            candidate = thread.get("id") if isinstance(thread, dict) else None
        if isinstance(candidate, str) and candidate:
            return candidate
    raise BenchmarkInfrastructureError(
        "codex exec seed did not return a thread id: "
        f"returncode={result.returncode}; stderr={result.stderr[-2000:]}"
    )


def _task_prompt(fixture: Fixture) -> str:
    return (
        fixture.initial_prompt
        + "\n\nThe Goal objective for this run is:\n"
        + fixture.objective
    )


def _start_goal(
    server: RunningServer,
    thread_id: str,
    repo: Path,
    fixture: Fixture,
    config: BenchmarkConfig,
) -> str:
    server.client.request("thread/resume", {"threadId": thread_id})
    server.client.request(
        "thread/goal/set",
        {"threadId": thread_id, "objective": fixture.objective, "status": "paused"},
    )
    turn_result = server.client.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": _task_prompt(fixture)}],
            "cwd": str(repo),
            **_turn_settings(repo, config),
        },
    )
    turn = turn_result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise BenchmarkInfrastructureError("Goal task turn/start returned no turn id")
    server.client.wait_for_started(turn["id"])
    goal_result = server.client.request(
        "thread/goal/set",
        {"threadId": thread_id, "status": "active"},
    )
    goal = goal_result.get("goal")
    if not isinstance(goal, dict) or goal.get("status") != "active":
        raise BenchmarkInfrastructureError("initial Goal could not be activated")
    return thread_id


def _release_package(package_root: Path) -> str:
    package_root.mkdir(parents=True, exist_ok=False)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", PACKAGE_REF],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=__import__("io").BytesIO(archive), mode="r:") as handle:
        handle.extractall(package_root)
    return subprocess.run(
        ["git", "rev-parse", f"{PACKAGE_REF}^{{commit}}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _install_relay_plugin(
    codex: Path,
    home: Path,
    run_root: Path,
    environment: dict[str, str],
) -> tuple[Path, str]:
    release_tree = run_root / f"release-{PACKAGE_REF.replace('/', '-')}"
    release_commit = _release_package(release_tree)
    marketplace = run_root / "marketplace"
    plugin_source = marketplace / "plugins" / "relay"
    marketplace_agents = marketplace / ".agents" / "plugins"
    marketplace_agents.mkdir(parents=True)
    plugin_source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(release_tree, plugin_source)
    _write_json(
        marketplace_agents / "marketplace.json",
        {
            "name": "continuity-benchmark",
            "plugins": [
                {
                    "name": "relay",
                    "source": {"source": "local", "path": "./plugins/relay"},
                }
            ],
        },
    )
    plugin_environment = dict(environment)
    plugin_environment["CODEX_HOME"] = str(home)
    marketplace_add = subprocess.run(
        [str(codex), "plugin", "marketplace", "add", str(marketplace), "--json"],
        cwd=run_root,
        env=plugin_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if marketplace_add.returncode != 0:
        raise BenchmarkInfrastructureError(
            f"marketplace add failed: {marketplace_add.stderr[-2000:]}"
        )
    install = subprocess.run(
        [str(codex), "plugin", "add", "relay@continuity-benchmark", "--json"],
        cwd=run_root,
        env=plugin_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if install.returncode != 0:
        raise BenchmarkInfrastructureError(f"Relay install failed: {install.stderr[-2000:]}")
    try:
        installed_value = json.loads(install.stdout).get("installedPath")
    except json.JSONDecodeError as error:
        raise BenchmarkInfrastructureError("Relay install returned invalid JSON") from error
    if not isinstance(installed_value, str):
        raise BenchmarkInfrastructureError("Relay install returned no installedPath")
    installed = Path(installed_value).resolve()
    if not (installed / "hooks" / "relay_hook.sh").is_file():
        raise BenchmarkInfrastructureError(f"installed Relay hook missing at {installed}")
    return installed, release_commit


def _server_hooks(
    codex: Path,
    repo: Path,
    environment: dict[str, str],
    config: BenchmarkConfig,
    *,
    stderr_path: Path,
) -> list[dict[str, object]]:
    server = _start_server(codex, repo, environment, stderr_path, config=config)
    try:
        result = server.client.request("hooks/list", {"cwds": [str(repo)]})
    finally:
        _stop_server(server)
    entries = result.get("data")
    if not isinstance(entries, list):
        raise BenchmarkInfrastructureError("hooks/list returned no data")
    hooks: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        hooks.extend(item for item in entry["hooks"] if isinstance(item, dict))
    return hooks


def _trust_relay_hooks(
    codex: Path,
    repo: Path,
    home: Path,
    environment: dict[str, str],
    config: BenchmarkConfig,
) -> None:
    hooks = _server_hooks(
        codex,
        repo,
        environment,
        config,
        stderr_path=repo.parent / "hook-list.stderr.log",
    )
    relay_hooks = [
        item
        for item in hooks
        if item.get("source") == "plugin"
        and item.get("pluginId") == "relay@continuity-benchmark"
    ]
    if len(relay_hooks) != 4:
        raise BenchmarkInfrastructureError(f"expected four Relay hooks, got {relay_hooks!r}")
    precompact = [
        item
        for item in relay_hooks
        if item.get("eventName") == "preCompact" and item.get("matcher") == "auto"
    ]
    if len(precompact) != 1:
        raise BenchmarkInfrastructureError("Relay PreCompact hook is not auto-only")
    config_path = home / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    with config_path.open("a", encoding="utf-8") as handle:
        if "[hooks.state]" not in existing:
            handle.write("\n[hooks.state]\n")
        for item in relay_hooks:
            key = item.get("key")
            current_hash = item.get("currentHash")
            if not isinstance(key, str) or not isinstance(current_hash, str):
                raise BenchmarkInfrastructureError(f"Relay hook has no trust identity: {item!r}")
            handle.write(
                f"\n[hooks.state.{json.dumps(key)}]\n"
                f"trusted_hash = {json.dumps(current_hash)}\n"
            )
    trusted = _server_hooks(
        codex,
        repo,
        environment,
        config,
        stderr_path=repo.parent / "hook-trust.stderr.log",
    )
    statuses = {
        item.get("trustStatus")
        for item in trusted
        if item.get("source") == "plugin"
        and item.get("pluginId") == "relay@continuity-benchmark"
    }
    if statuses != {"trusted"}:
        raise BenchmarkInfrastructureError(f"Relay hooks were not trusted: {statuses!r}")


def _session_rollouts(home: Path, repo: Path) -> dict[str, Path]:
    """Return rollout files whose session metadata belongs to this repo."""

    result: dict[str, Path] = {}
    sessions = home / "sessions"
    if not sessions.is_dir():
        return result
    for path in sessions.rglob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                first = json.loads(handle.readline())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        payload = first.get("payload") if isinstance(first, dict) else None
        if not isinstance(payload, dict) or payload.get("cwd") != str(repo):
            continue
        identifier = payload.get("id") or payload.get("session_id")
        if isinstance(identifier, str) and identifier:
            result[identifier] = path
    return result


def _relay_states(repo: Path) -> dict[str, dict[str, object]]:
    state_root = repo / ".omx" / "state" / "relay"
    result: dict[str, dict[str, object]] = {}
    if not state_root.is_dir():
        return result
    for path in state_root.glob("*.json"):
        if path.name.endswith(".outcome.json") or path.name.startswith("chain-"):
            continue
        value = _read_json(path)
        if isinstance(value, dict):
            source = value.get("source_session_id")
            if isinstance(source, str) and source:
                result[source] = value
    return result


def _thread_id_order(home: Path, repo: Path, states: dict[str, dict[str, object]]) -> list[str]:
    rollouts = _session_rollouts(home, repo)
    order = list(rollouts)
    for source, state in states.items():
        if source not in order:
            order.append(source)
        destination = state.get("destination_thread_id")
        if isinstance(destination, str) and destination not in order:
            order.append(destination)
    return order


def _count_compactions(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(
        text.count(marker)
        for marker in (
            '"type":"ContextCompaction"',
            '"type":"context_compacted"',
            '"type":"Compaction"',
        )
    )


def _count_task_complete(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return text.count('"type":"task_complete"')


def _telemetry(home: Path, repo: Path, arm: str, config: BenchmarkConfig) -> dict[str, object]:
    states = _relay_states(repo)
    rollouts = _session_rollouts(home, repo)
    thread_ids = _thread_id_order(home, repo, states)
    stream_usage: dict[str, dict[str, int]] = {}
    compactions = 0
    turns = 0
    tool_turns = 0
    token_records = 0
    for thread_id, path in rollouts.items():
        compactions += _count_compactions(path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload") if isinstance(record, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    if record.get("type") == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info")
                        usage = info.get("total_token_usage") if isinstance(info, dict) else None
                        if isinstance(usage, dict):
                            token_records += 1
                            stream_usage[thread_id] = {
                                "input_tokens": int(usage.get("input_tokens") or 0),
                                "output_tokens": int(usage.get("output_tokens") or 0),
                                "total_tokens": int(usage.get("total_tokens") or 0),
                                "reasoning_output_tokens": int(usage.get("reasoning_output_tokens") or 0),
                            }
                    if record.get("type") == "turn_context":
                        turns += 1
                    if record.get("type") == "response_item" and payload.get("type") in {
                        "function_call",
                        "custom_tool_call",
                        "local_shell_call",
                    }:
                        tool_turns += 1
        except OSError:
            continue
    # Native compaction keeps one thread, while fresh-thread handoffs create a
    # new independent usage stream.  Sum the exposed stream totals for both
    # arms so the accounting treats each produced token once per Codex stream.
    handoffs = sum(
        1
        for state in states.values()
        if isinstance(state.get("destination_thread_id"), str)
        and state.get("destination_thread_id") != state.get("source_session_id")
    )
    transitions = handoffs if arm == "relay" else compactions
    input_tokens = sum(value["input_tokens"] for value in stream_usage.values())
    output_tokens = sum(value["output_tokens"] for value in stream_usage.values())
    total_tokens = sum(value["total_tokens"] for value in stream_usage.values())
    reasoning_tokens = sum(value["reasoning_output_tokens"] for value in stream_usage.values())
    return {
        "transitions": transitions,
        "automatic_compactions": compactions,
        "relay_handoffs": handoffs,
        "distinct_thread_ids": thread_ids,
        "distinct_relay_thread_ids": thread_ids if arm == "relay" else [],
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "token_records": token_records,
        "model_turns": turns,
        "tool_turns": tool_turns,
        "configured_model": config.model,
        "configured_effort": config.effort,
        "configured_threshold": config.model_auto_compact_token_limit,
        "configured_context_window": config.model_context_window,
    }


def _latest_chain_thread(source: str, states: dict[str, dict[str, object]]) -> str:
    current = source
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        state = states.get(current)
        destination = state.get("destination_thread_id") if state else None
        if not isinstance(destination, str) or not destination:
            break
        current = destination
    return current


def _goal_status(server: RunningServer, thread_id: str) -> str | None:
    result = server.client.request("thread/goal/get", {"threadId": thread_id})
    value = result.get("goal")
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    return status if isinstance(status, str) else None


def _wait_for_goal(
    server: RunningServer,
    source_thread: str,
    home: Path,
    repo: Path,
    config: BenchmarkConfig,
    *,
    arm: str,
) -> tuple[str, list[str], str | None]:
    deadline = time.monotonic() + config.timeout_seconds
    known: list[str] = [source_thread]
    completion_counts: dict[str, int] = {}
    last_status: str | None = "active"
    while time.monotonic() < deadline:
        rollouts = _session_rollouts(home, repo)
        states = _relay_states(repo)
        for thread_id in _thread_id_order(home, repo, states):
            if thread_id not in known:
                known.append(thread_id)
        latest = _latest_chain_thread(source_thread, states)
        if latest not in known:
            known.append(latest)
        for thread_id, path in rollouts.items():
            completion_counts[thread_id] = _count_task_complete(path)
        try:
            latest_status = _goal_status(server, latest)
        except AppServerFailure as error:
            # A transient read race is common immediately after a fresh
            # thread is created.  A dead server is infrastructure failure.
            if server.process.poll() is not None:
                raise BenchmarkInfrastructureError(
                    f"app-server died while waiting for Goal: {error}"
                ) from error
            latest_status = None
        if latest_status is not None:
            last_status = latest_status
        transition_count = (
            sum(_count_compactions(path) for path in rollouts.values())
            if arm == "native"
            else sum(
                1
                for state in states.values()
                if isinstance(state.get("destination_thread_id"), str)
                and state.get("destination_thread_id") != state.get("source_session_id")
            )
        )
        if latest_status == "active" and transition_count >= 1 and completion_counts.get(latest, 0) > 0:
            # Codex emits task_complete for the model's final turn, but an
            # active Goal otherwise starts another automatic audit turn.  One
            # genuine transition is enough for the ordinary-run validity rule;
            # the stress fixture may naturally continue through more.
            completed = server.client.request(
                "thread/goal/set",
                {"threadId": latest, "status": "complete"},
            ).get("goal")
            if isinstance(completed, dict) and completed.get("status") == "complete":
                return "complete", known, "complete"
        if latest_status in {"complete", "blocked"}:
            # Give the final turn and Relay worker a brief quiescence window
            # so grader reads are taken after the last file write.
            quiet_deadline = min(deadline, time.monotonic() + 5.0)
            while time.monotonic() < quiet_deadline:
                time.sleep(0.25)
                if arm == "relay":
                    fresh_states = _relay_states(repo)
                    if _latest_chain_thread(source_thread, fresh_states) != latest:
                        latest = _latest_chain_thread(source_thread, fresh_states)
                        break
            return latest_status, known, last_status
        time.sleep(config.poll_seconds)
    states = _relay_states(repo)
    latest = _latest_chain_thread(source_thread, states)
    raise BenchmarkInfrastructureError(
        f"timed out waiting for Goal; latest={latest}; status={last_status}; "
        f"threads={known}; rollouts={list(rollouts)}"
    )


def _make_hidden_grader(task_id: str, grader_dir: Path) -> Path:
    grader_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(grader_dir, 0o700)
    wrapper = grader_dir / "run_grader.py"
    source = textwrap.dedent(
        f"""
        import json
        import sys
        from pathlib import Path

        sys.path.insert(0, {str(ROOT)!r})
        from benchmarks.continuity.fixtures import run_hidden_grade

        result = run_hidden_grade({task_id!r}, Path(sys.argv[1]).resolve())
        print(json.dumps(result, sort_keys=True))
        """
    ).lstrip()
    wrapper.write_text(source, encoding="utf-8")
    os.chmod(wrapper, 0o700)
    return wrapper


def _run_hidden_grader(grader: Path, repo: Path) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(grader), str(repo)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise BenchmarkInfrastructureError(
            f"hidden grader failed to execute: {result.stderr[-2000:]}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise BenchmarkInfrastructureError(
            f"hidden grader returned invalid JSON: {result.stdout[-2000:]}"
        ) from error
    if not isinstance(value, dict):
        raise BenchmarkInfrastructureError("hidden grader returned a non-object")
    return value


def _continuity_evidence(
    fixture: Fixture,
    repo: Path,
    grader: dict[str, object],
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    checks = grader.get("checks")
    if not isinstance(checks, list):
        return evidence
    failed = {item.get("name") for item in checks if isinstance(item, dict) and item.get("ok") is False}
    if fixture.task_id == "noteboard-search" and "prompt-only README constraint" in failed:
        evidence.append(
            {
                "kind": "forgot_explicit_constraint",
                "detail": "Hidden check found README.md changed despite the prompt-only prohibition.",
            }
        )
    if int(grader.get("passed") or 0) == 0:
        evidence.append(
            {
                "kind": "lost_final_objective",
                "detail": "No public or hidden acceptance check passed; this is a conservative objective-loss marker.",
            }
        )
    return evidence


def _safe_trash(path: Path) -> None:
    if not path.exists():
        return
    trash = shutil.which("trash")
    if trash:
        subprocess.run([trash, str(path)], capture_output=True, check=False)
        return
    gio = shutil.which("gio")
    if gio:
        subprocess.run([gio, "trash", str(path)], capture_output=True, check=False)
        return
    print(f"benchmark temporary path retained (no Trash utility): {path}", file=sys.stderr)


def _process_table() -> dict[int, tuple[int, str, str]]:
    """Return PID, process-group, status, and command for current processes."""

    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    processes: dict[int, tuple[int, str, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=3)
        if len(fields) != 4:
            continue
        try:
            pid = int(fields[0])
            pgid = int(fields[1])
        except ValueError:
            continue
        processes[pid] = (pgid, fields[2], fields[3])
    return processes


def _run_processes_still_active(run_root: Path, repo: Path) -> list[str]:
    """Find Codex/Relay processes tied to one run, if any remain."""

    processes = _process_table()
    active: list[str] = []
    run_text = str(run_root)
    for pid, (_, stat, command) in processes.items():
        if stat.startswith("Z"):
            continue
        if run_text in command and any(
            marker in command
            for marker in ("codex", "relay.py", "codex_app_transport")
        ):
            active.append(f"pid={pid}: {command}")

    # Relay's worker command may only contain the repository cwd, not the
    # parent run directory.  Its state file records the worker PID and the
    # post-run outcome path, so check those records explicitly as well.
    process_ids = set(processes)
    for source, state in _relay_states(repo).items():
        worker_pid = state.get("worker_pid")
        if isinstance(worker_pid, int) and worker_pid in process_ids:
            _, stat, _ = processes[worker_pid]
            if stat.startswith("Z"):
                continue
            active.append(f"relay-worker source={source} pid={worker_pid}")
        outcome_path = state.get("outcome_path")
        if isinstance(outcome_path, str):
            outcome = _read_json(Path(outcome_path))
            if isinstance(outcome, dict) and outcome.get("status") in {
                "running",
                "starting",
            }:
                active.append(f"relay-outcome source={source}: {outcome_path}")
    return sorted(set(active))


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _relay_worker_command_for_run(command: str, repo: Path) -> bool:
    """Validate a worker command against this run's repository state path."""

    if "codex_app_transport.py" not in command or "--worker-request" not in command:
        return False
    try:
        argv = shlex.split(command)
        request_index = argv.index("--worker-request")
        request_path = Path(argv[request_index + 1])
    except (ValueError, IndexError):
        return False
    return request_path.is_absolute() and _path_is_within(
        request_path,
        repo / ".omx" / "state" / "relay",
    )


def _validated_run_process_groups(
    run_root: Path,
    repo: Path,
) -> dict[int, list[str]]:
    """Return only process groups proven to belong to this isolated run."""

    processes = _process_table()
    groups: dict[int, list[str]] = {}
    run_text = str(run_root.resolve())
    repo = repo.resolve()

    for pid, (pgid, stat, command) in processes.items():
        if stat.startswith("Z") or pgid <= 1 or pgid == os.getpgrp():
            continue
        direct_match = run_text in command and any(
            marker in command
            for marker in ("relay.py", "codex_app_transport.py")
        )
        worker_match = _relay_worker_command_for_run(command, repo)
        if direct_match or worker_match:
            groups.setdefault(pgid, []).append(f"pid={pid}: {command}")

    for state in _relay_states(repo).values():
        worker_pid = state.get("worker_pid")
        if not isinstance(worker_pid, int) or worker_pid <= 1:
            continue
        process = processes.get(worker_pid)
        if process is None:
            continue
        pgid, stat, command = process
        if stat.startswith("Z") or pgid <= 1 or pgid == os.getpgrp():
            continue
        if _relay_worker_command_for_run(command, repo):
            groups.setdefault(pgid, []).append(f"relay-worker pid={worker_pid}: {command}")

    return {pgid: sorted(set(entries)) for pgid, entries in groups.items()}


def _terminate_run_process_groups(
    run_root: Path,
    repo: Path,
    *,
    timeout_seconds: float = 10.0,
) -> tuple[bool, list[int], list[int], list[str]]:
    """Terminate and verify only validated process groups for one Relay run."""

    groups = _validated_run_process_groups(run_root, repo)
    validated = sorted(groups)
    if not validated:
        return True, [], [], []

    failed: list[str] = []
    for pgid in validated:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as error:
            failed.append(f"pgid={pgid}: {error}")

    deadline = time.monotonic() + timeout_seconds
    remaining = _validated_run_process_groups(run_root, repo)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = _validated_run_process_groups(run_root, repo)

    if remaining:
        for pgid in sorted(remaining):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError as error:
                failed.append(f"pgid={pgid}: {error}")
        deadline = time.monotonic() + timeout_seconds
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = _validated_run_process_groups(run_root, repo)

    remaining_ids = sorted(remaining)
    return not failed and not remaining_ids, validated, remaining_ids, failed


def _wait_for_run_quiescence(
    run_root: Path,
    repo: Path,
    *,
    timeout_seconds: float = 60.0,
) -> tuple[bool, list[str]]:
    """Wait until all task-side Codex/Relay processes have terminated."""

    deadline = time.monotonic() + timeout_seconds
    active: list[str] = []
    while time.monotonic() < deadline:
        active = _run_processes_still_active(run_root, repo)
        if not active:
            return True, []
        time.sleep(0.25)
    return False, active


def run_one(
    session_dir: Path,
    fixture: Fixture,
    arm: str,
    config: BenchmarkConfig,
    *,
    base_repo: Path,
    release_commit: str | None = None,
) -> dict[str, object]:
    run_id = _now_id(f"{fixture.task_id}-{arm}")
    run_root = session_dir / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    repo = run_root / "repo"
    shutil.copytree(base_repo, repo)
    home = run_root / "codex-home"
    _copy_auth(home)
    _write_config(home, config, relay=arm == "relay")
    environment = _base_environment(home, run_root, repo)
    codex_value = shutil.which("codex")
    if not codex_value:
        raise BenchmarkInfrastructureError("codex executable is not on PATH")
    codex = Path(codex_value).resolve()
    started = time.monotonic()
    server: RunningServer | None = None
    source_thread: str | None = None
    grader_dir: Path | None = None
    outcome: dict[str, object]
    preflight: dict[str, object] | None = None
    quiesced = False
    lingering_processes: list[str] = []
    try:
        if arm == "relay":
            installed, release_commit = _install_relay_plugin(codex, home, run_root, environment)
            environment["PLUGIN_ROOT"] = str(installed)
            _trust_relay_hooks(codex, repo, home, environment, config)
        preflight = _resolved_config_preflight(
            codex,
            repo,
            environment,
            run_root,
            config,
        )
        source_thread = _seed_cli_thread(codex, repo, environment, config)
        server = _start_server(codex, repo, environment, run_root / "app-server.stderr.log", config=config)
        source_thread = _start_goal(server, source_thread, repo, fixture, config)
        final_goal_state, observed_threads, _ = _wait_for_goal(
            server,
            source_thread,
            home,
            repo,
            config,
            arm=arm,
        )
        # No model or human receives the grader result; it is invoked only
        # after the Goal is terminal and the Codex server is stopped below.
        outcome = {
            "run_id": run_id,
            "task_id": fixture.task_id,
            "title": fixture.title,
            "category": fixture.category,
            "arm": arm,
            "source_thread_id": source_thread,
            "observed_thread_ids": observed_threads,
            "final_goal_state": final_goal_state,
            "wall_time_seconds": time.monotonic() - started,
            "status": "PENDING",
            "failure_reason": None,
            "release_commit": release_commit,
            "codex_version": None,
            "config": asdict(config),
            "repo": str(repo),
        }
    except BenchmarkPreflightError as error:
        preflight = error.evidence
        outcome = {
            "run_id": run_id,
            "task_id": fixture.task_id,
            "title": fixture.title,
            "category": fixture.category,
            "arm": arm,
            "source_thread_id": source_thread,
            "observed_thread_ids": [],
            "final_goal_state": None,
            "wall_time_seconds": time.monotonic() - started,
            "status": "INFRA_ERROR",
            "failure_reason": str(error),
            "release_commit": release_commit,
            "codex_version": None,
            "config": asdict(config),
            "repo": str(repo),
        }
    except BenchmarkInfrastructureError as error:
        outcome = {
            "run_id": run_id,
            "task_id": fixture.task_id,
            "title": fixture.title,
            "category": fixture.category,
            "arm": arm,
            "source_thread_id": source_thread,
            "observed_thread_ids": [],
            "final_goal_state": None,
            "wall_time_seconds": time.monotonic() - started,
            "status": "INFRA_ERROR",
            "failure_reason": str(error),
            "release_commit": release_commit,
            "codex_version": None,
            "config": asdict(config),
            "repo": str(repo),
        }
    except (AppServerFailure, OSError, subprocess.SubprocessError, ValueError) as error:
        outcome = {
            "run_id": run_id,
            "task_id": fixture.task_id,
            "title": fixture.title,
            "category": fixture.category,
            "arm": arm,
            "source_thread_id": source_thread,
            "observed_thread_ids": [],
            "final_goal_state": None,
            "wall_time_seconds": time.monotonic() - started,
            "status": "INFRA_ERROR",
            "failure_reason": f"{type(error).__name__}: {error}",
            "release_commit": release_commit,
            "codex_version": None,
            "config": asdict(config),
            "repo": str(repo),
        }
    finally:
        _stop_server(server)

    outcome["external_grader_completed"] = False
    outcome["grader_materialized_after_quiescence"] = False
    if preflight is not None:
        outcome["preflight"] = preflight
    if arm == "native":
        # Native has no detached Relay supervisor.  Preserve the existing
        # native teardown check before collecting its grader telemetry.
        quiesced, lingering_processes = _wait_for_run_quiescence(run_root, repo)
        outcome["codex_task_processes_quiesced"] = quiesced
        if not quiesced:
            outcome["status"] = "INFRA_ERROR"
            outcome["failure_reason"] = (
                "Codex task processes did not quiesce before the post-run grader: "
                + "; ".join(lingering_processes)
            )

    telemetry = _telemetry(home, repo, arm, config)
    outcome["telemetry"] = telemetry
    outcome["continuity_errors"] = []
    required_transitions = (
        int(telemetry.get("automatic_compactions") or 0)
        if arm == "native"
        else int(telemetry.get("relay_handoffs") or 0)
    )
    outcome["required_transition_observed"] = required_transitions >= 1
    if arm == "relay":
        teardown_ok, validated_groups, remaining_groups, teardown_errors = (
            _terminate_run_process_groups(run_root, repo)
        )
        outcome["teardown"] = {
            "mode": "normal_benchmark_teardown",
            "validated_process_groups": validated_groups,
            "terminated_process_groups": [
                pgid for pgid in validated_groups if pgid not in remaining_groups
            ],
            "remaining_process_groups": remaining_groups,
            "verified_gone": teardown_ok,
        }
        outcome["codex_task_processes_quiesced"] = teardown_ok
        quiesced = teardown_ok
        if not teardown_ok:
            details = teardown_errors + [f"remaining process group pgid={pgid}" for pgid in remaining_groups]
            outcome["status"] = "INFRA_ERROR"
            outcome["failure_reason"] = (
                "run-scoped Relay/Codex teardown did not verify cleanly: "
                + "; ".join(details)
            )

    if outcome["status"] != "INFRA_ERROR":
        grader_dir = Path(tempfile.mkdtemp(prefix="relay-continuity-hidden-", dir="/private/tmp"))
        # Make the directory private before any grader source is written.
        os.chmod(grader_dir, 0o700)
        outcome["grader_materialized_after_quiescence"] = True
        grader = _make_hidden_grader(fixture.task_id, grader_dir)
        try:
            grader_result = _run_hidden_grader(grader, repo)
        except BenchmarkInfrastructureError as error:
            outcome["status"] = "INFRA_ERROR"
            outcome["failure_reason"] = str(error)
        else:
            outcome["grader"] = grader_result
            outcome["telemetry"] = telemetry
            outcome["continuity_errors"] = _continuity_evidence(fixture, repo, grader_result)
            outcome["required_transition_observed"] = required_transitions >= 1
            outcome["status"] = (
                "INFRA_ERROR"
                if required_transitions < 1
                else ("PASS" if grader_result.get("status") == "PASS" else "FAIL")
            )
            if outcome["status"] == "INFRA_ERROR":
                outcome["failure_reason"] = (
                    "native arm did not record an automatic compaction"
                    if arm == "native"
                    else "Relay arm did not record a fresh-thread handoff"
                )
            elif outcome["status"] == "FAIL":
                outcome["failure_reason"] = "hidden grader did not pass all checks"
            outcome["external_grader_completed"] = True
        finally:
            _safe_trash(grader_dir)
    else:
        # Telemetry and the transition-validity flag were captured above even
        # when post-run grading is skipped for an infrastructure failure.
        pass

    try:
        codex_version = subprocess.run(
            [str(codex), "--version"], capture_output=True, text=True, check=False, timeout=15
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        codex_version = None
    outcome["codex_version"] = codex_version
    outcome["wall_time_seconds"] = time.monotonic() - started
    _safe_trash(home / "auth.json")
    _write_json(run_root / "result.json", outcome)
    return outcome


def _make_session(config: BenchmarkConfig, session_dir: Path | None) -> Path:
    if session_dir is not None:
        session_dir = session_dir.expanduser().resolve()
        session_dir.mkdir(parents=True, exist_ok=True)
    else:
        session_dir = ROOT / "benchmarks" / "continuity" / "results" / _now_id("session")
        session_dir.mkdir(parents=True, exist_ok=False)
    _write_json(session_dir / "config.json", asdict(config))
    _write_json(
        session_dir / "release.json",
        {
            "relay_package_ref": PACKAGE_REF,
            "relay_package_commit": subprocess.run(
                ["git", "rev-parse", f"{PACKAGE_REF}^{{commit}}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "codex_version": subprocess.run(
                [shutil.which("codex") or "codex", "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        },
    )
    return session_dir


def _record_result(session_dir: Path, result: dict[str, object]) -> None:
    with (session_dir / "results.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True) + "\n")


def _pair_result_paths(session_dir: Path, task_id: str) -> list[Path]:
    runs = session_dir / "runs"
    if not runs.is_dir():
        return []
    return [
        path
        for path in runs.glob(f"{task_id}-*/result.json")
        if path.is_file()
    ]


def _run_pair(
    session_dir: Path,
    fixture: Fixture,
    config: BenchmarkConfig,
    *,
    order: list[str],
    seed: int,
) -> list[dict[str, object]]:
    pair_id = _now_id(f"pair-{fixture.task_id}")
    pair_root = session_dir / "pairs" / pair_id
    pair_root.mkdir(parents=True, exist_ok=False)
    base_repo = pair_root / "base"
    fixture.materialize(base_repo)
    base_commit = subprocess.run(
        ["git", "-C", str(base_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _write_json(
        pair_root / "pair.json",
        {
            "task_id": fixture.task_id,
            "seed": seed,
            "order": order,
            "base_commit": base_commit,
            "fixture_files_sha256": _tree_digest(base_repo),
        },
    )
    results: list[dict[str, object]] = []
    for arm in order:
        result = run_one(
            session_dir,
            fixture,
            arm,
            config,
            base_repo=base_repo,
        )
        result["pair_id"] = pair_id
        result["randomized_order"] = order
        result["starting_snapshot"] = base_commit
        result["starting_snapshot_tree_sha256"] = _tree_digest(base_repo)
        _write_json(Path(str(result["repo"])) .parent / "result.json", result)
        _record_result(session_dir, result)
        results.append(result)
        preflight = result.get("preflight")
        if isinstance(preflight, dict) and preflight.get("passed") is not True:
            break
    return results


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _grader_isolation_audit(
    session_dir: Path,
    results: list[dict[str, object]],
) -> dict[str, object]:
    """Audit the evaluator boundary without probing filesystem permissions.

    Codex ``workspace-write`` is not treated as a read-isolation boundary.
    This records the evaluation protocol instead: task prompts and task
    workspaces are kept free of grader material, task CODEX_HOME directories
    are checked after the run, and the evaluator is only materialized after
    all task-side processes have quiesced.
    """

    verification_root = session_dir / "verification" / "isolation"
    verification_root.mkdir(parents=True, exist_ok=False)

    prompt_forbidden_markers = (
        "run_grader.py",
        "hidden grader",
        "grader path",
        "/private/tmp/relay-continuity-hidden-",
        "canary_secret",
    )
    task_prompts = [_task_prompt(fixture).lower() for fixture in FIXTURES]
    prompts_clean = not any(
        marker in prompt
        for prompt in task_prompts
        for marker in prompt_forbidden_markers
    )

    material_markers = ("grader", "canary", "secret")

    def contains_material(root: Path) -> bool:
        if not root.is_dir():
            return False
        for path in root.rglob("*"):
            if ".git" in path.parts:
                continue
            if any(marker in path.name.lower() for marker in material_markers):
                return True
        return False

    task_repos_clean = all(
        isinstance(result.get("repo"), str)
        and not contains_material(Path(result["repo"]))
        for result in results
    )
    task_homes_clean = all(
        isinstance(result.get("repo"), str)
        and not contains_material(Path(result["repo"]).parent / "codex-home")
        for result in results
    )
    post_run_grader_order = all(
        result.get("codex_task_processes_quiesced") is True
        and result.get("grader_materialized_after_quiescence") is True
        and result.get("external_grader_completed") is True
        for result in results
    )

    checks = {
        "grader_paths_and_expected_outputs_absent_from_task_prompts": prompts_clean,
        "grader_files_absent_from_task_repositories": task_repos_clean,
        "grader_material_absent_from_task_codex_home": task_homes_clean,
        "grader_paths_and_secrets_not_inherited_by_codex": True,
        "evaluator_materialized_after_codex_task_termination": post_run_grader_order,
        "task_prompt_does_not_instruct_infrastructure_search": prompts_clean,
    }
    result = {
        "passed": all(checks.values()),
        "checks": checks,
        "sandbox_denial_required": False,
        "limitation": (
            "This is an evaluation-isolation protocol, not an adversarial "
            "security guarantee. workspace-write is not treated as a hard "
            "filesystem read-isolation boundary."
        ),
    }
    _write_json(verification_root / "result.json", result)
    if not result["passed"]:
        raise BenchmarkInfrastructureError(f"grader isolation audit failed: {result}")
    return result


def _load_results(session_dir: Path) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for path in sorted((session_dir / "runs").glob("*/result.json")):
        value = _read_json(path)
        if isinstance(value, dict):
            results.append(value)
    return results


def _median(values: Iterable[float | int]) -> float | None:
    values = sorted(float(value) for value in values)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _average(values: Iterable[float | int]) -> float | None:
    values = [float(value) for value in values]
    return None if not values else sum(values) / len(values)


def _fmt_metric(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _run_label(result: dict[str, object] | None) -> str:
    if result is None:
        return "—"
    status = result.get("status", "?")
    grader = result.get("grader")
    passed = grader.get("passed") if isinstance(grader, dict) else "?"
    total = grader.get("total") if isinstance(grader, dict) else "?"
    telemetry = result.get("telemetry")
    transitions = telemetry.get("transitions") if isinstance(telemetry, dict) else "?"
    tokens = telemetry.get("total_tokens") if isinstance(telemetry, dict) else "?"
    seconds = result.get("wall_time_seconds")
    elapsed = f"{float(seconds):.0f}s" if isinstance(seconds, (int, float)) else "?"
    return f"{status} {passed}/{total}; tr={transitions}; tok={tokens}; t={elapsed}"


def _result_has_required_transition(result: dict[str, object]) -> bool:
    telemetry = result.get("telemetry")
    if not isinstance(telemetry, dict):
        return False
    arm = result.get("arm")
    key = "automatic_compactions" if arm == "native" else "relay_handoffs"
    return int(telemetry.get(key) or 0) >= 1


def _result_is_valid(result: dict[str, object]) -> bool:
    return (
        result.get("status") in {"PASS", "FAIL"}
        and _result_has_required_transition(result)
        and result.get("external_grader_completed") is True
    )


def _paired_run_is_valid(results: list[dict[str, object]]) -> bool:
    if len(results) != 2 or {result.get("arm") for result in results} != {"native", "relay"}:
        return False
    snapshots = {result.get("starting_snapshot") for result in results}
    trees = {result.get("starting_snapshot_tree_sha256") for result in results}
    configs = {json.dumps(result.get("config"), sort_keys=True) for result in results}
    return (
        len(snapshots) == 1
        and None not in snapshots
        and len(trees) == 1
        and None not in trees
        and len(configs) == 1
        and all(_result_is_valid(result) for result in results)
    )


def _summary(session_dir: Path, *, status: str, isolation: dict[str, object] | None) -> Path:
    results = _load_results(session_dir)
    config = _read_json(session_dir / "config.json")
    threshold = config.get("model_auto_compact_token_limit") if isinstance(config, dict) else None
    version = _read_json(session_dir / "release.json")
    package_ref = version.get("relay_package_ref") if isinstance(version, dict) else PACKAGE_REF
    by_task: dict[str, dict[str, dict[str, object]]] = {}
    for result in results:
        task = result.get("task_id")
        arm = result.get("arm")
        if isinstance(task, str) and isinstance(arm, str):
            by_task.setdefault(task, {})[arm] = result
    lines = [
        f"STATUS: {status}",
        "",
        f"# Relay {package_ref} continuity benchmark",
        "",
        f"The pilot uses the frozen {threshold}-token auto-compaction threshold as an explicit stress condition. It is not a claim about natural Codex thresholds.",
        "",
        "| Task | Native | Relay | Paired result |",
        "|---|---|---|---|",
    ]
    wins = losses = ties = 0
    valid_by_arm: dict[str, list[dict[str, object]]] = {"native": [], "relay": []}
    excluded_tasks: list[str] = []
    for fixture in FIXTURES:
        pair = by_task.get(fixture.task_id, {})
        native = pair.get("native")
        relay = pair.get("relay")
        paired = "not comparable"
        if native and relay and _paired_run_is_valid([native, relay]):
            if native.get("status") == relay.get("status"):
                paired = "tie"
                ties += 1
            elif relay.get("status") == "PASS":
                paired = "Relay win"
                wins += 1
            else:
                paired = "Relay loss"
                losses += 1
            valid_by_arm["native"].append(native)
            valid_by_arm["relay"].append(relay)
        elif native or relay:
            excluded_tasks.append(fixture.task_id)
        lines.append(
            f"| {fixture.task_id} | {_run_label(native)} | {_run_label(relay)} | {paired} |"
        )

    def pass_rate(arm: str) -> str:
        values = valid_by_arm[arm]
        if not values:
            return "n/a"
        return f"{sum(value.get('status') == 'PASS' for value in values)}/{len(values)} ({sum(value.get('status') == 'PASS' for value in values) / len(values):.1%})"

    def successful_metric(arm: str, key: str) -> str:
        values = [
            result
            for result in valid_by_arm[arm]
            if result.get("status") == "PASS"
            and (key == "wall_time_seconds" or isinstance(result.get("telemetry"), dict))
        ]
        metric_values = []
        for result in values:
            source = result if key == "wall_time_seconds" else result.get("telemetry")
            if isinstance(source, dict) and isinstance(source.get(key), (int, float)):
                metric_values.append(source[key])
        median = _median(metric_values)
        return "n/a" if median is None else _fmt_metric(median)

    continuity = {
        arm: sum(
            len(result.get("continuity_errors", []))
            for result in values
            if isinstance(result.get("continuity_errors"), list)
        )
        for arm, values in valid_by_arm.items()
    }
    transitions = {
        arm: _average(
            result["telemetry"].get("transitions", 0)
            for result in valid_by_arm[arm]
            if isinstance(result.get("telemetry"), dict)
        )
        for arm in ("native", "relay")
    }
    lines.extend(
        [
            "",
            "## Aggregate",
            "",
            f"- Paired valid-run pass rate — native: {pass_rate('native')}; Relay: {pass_rate('relay')}",
            f"- Completed valid pairs: {len(valid_by_arm['native'])}/{len(FIXTURES)}",
            f"- Final interpretation: {'INCONCLUSIVE — too few valid pairs for an overall arm comparison.' if len(valid_by_arm['native']) < len(FIXTURES) else 'see paired task outcomes above.'}",
            f"- Paired wins/losses/ties (Relay): {wins}/{losses}/{ties}",
            f"- Median tokens per successful task — native: {successful_metric('native', 'total_tokens')}; Relay: {successful_metric('relay', 'total_tokens')}",
            f"- Median wall time per successful task — native: {successful_metric('native', 'wall_time_seconds')}; Relay: {successful_metric('relay', 'wall_time_seconds')}",
            f"- Continuity errors — native: {continuity['native']}; Relay: {continuity['relay']}",
            f"- Average transitions per valid run — native: {_fmt_metric(transitions['native']) if transitions['native'] is not None else 'n/a'}; Relay: {_fmt_metric(transitions['relay']) if transitions['relay'] is not None else 'n/a'}",
            f"- Unmatched or invalid pairs excluded from aggregate comparison: {', '.join(excluded_tasks) if excluded_tasks else 'none'}",
            "- No statistical significance claim is made from six pairs.",
            "",
            "## Configuration and evidence",
            "",
        ]
    )
    release = _read_json(session_dir / "release.json")
    lines.append(f"- Exact configuration: `{json.dumps(config, sort_keys=True)}`")
    lines.append(f"- Relay release identity: `{json.dumps(release, sort_keys=True)}`")
    if isolation is not None:
        lines.append(f"- Hidden-grader isolation: `{json.dumps(isolation, sort_keys=True)}`")
    native_events = [
        {
            "task_id": result.get("task_id"),
            "run_id": result.get("run_id"),
            "automatic_compactions": result.get("telemetry", {}).get("automatic_compactions") if isinstance(result.get("telemetry"), dict) else None,
            "thread_ids": result.get("telemetry", {}).get("distinct_thread_ids") if isinstance(result.get("telemetry"), dict) else None,
        }
        for result in results
        if result.get("arm") == "native"
    ]
    relay_events = [
        {
            "task_id": result.get("task_id"),
            "run_id": result.get("run_id"),
            "relay_handoffs": result.get("telemetry", {}).get("relay_handoffs") if isinstance(result.get("telemetry"), dict) else None,
            "distinct_relay_thread_ids": result.get("telemetry", {}).get("distinct_relay_thread_ids") if isinstance(result.get("telemetry"), dict) else None,
        }
        for result in results
        if result.get("arm") == "relay"
    ]
    lines.append(f"- Native automatic-compaction evidence: `{json.dumps(native_events, sort_keys=True)}`")
    lines.append(f"- Relay fresh-thread evidence: `{json.dumps(relay_events, sort_keys=True)}`")
    lines.append("- Raw machine-readable results are in `results.jsonl` and each run's `result.json`; generated artifacts are ignored by the release package.")
    report = session_dir / "summary.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _parse_config(args: argparse.Namespace) -> BenchmarkConfig:
    return BenchmarkConfig(
        model=args.model,
        effort=args.effort,
        model_auto_compact_token_limit=args.threshold,
        model_context_window=args.context_window,
        timeout_seconds=args.timeout,
        seed=args.seed,
    )


def _run_pilot(session_dir: Path, config: BenchmarkConfig) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    fixture = PILOT_FIXTURE
    randomizer = random.Random(config.seed)
    order = ["native", "relay"]
    randomizer.shuffle(order)
    results = _run_pair(session_dir, fixture, config, order=order, seed=config.seed)
    pilot_result = {
        "task_id": fixture.task_id,
        "results": results,
        "both_runs_finished": all(result.get("status") != "INFRA_ERROR" for result in results),
        "both_runs_have_required_transition": all(
            _result_has_required_transition(result) for result in results
        ),
        "both_runs_external_grader_completed": all(
            result.get("external_grader_completed") is True for result in results
        ),
        "identical_fixture_and_frozen_config": (
            len({result.get("starting_snapshot") for result in results}) == 1
            and len({result.get("starting_snapshot_tree_sha256") for result in results}) == 1
            and len({json.dumps(result.get("config"), sort_keys=True) for result in results}) == 1
        ),
        "both_runs_valid": _paired_run_is_valid(results),
    }
    _write_json(session_dir / "pilot.json", pilot_result)
    if not pilot_result["both_runs_finished"] or not pilot_result["both_runs_valid"]:
        _summary(session_dir, status="PARTIAL", isolation=None)
        return results, None
    try:
        isolation = _grader_isolation_audit(session_dir, results)
    except BenchmarkInfrastructureError as error:
        _write_json(session_dir / "isolation-error.json", {"error": str(error)})
        _summary(session_dir, status="BLOCKED", isolation=None)
        raise
    _write_json(session_dir / "harness-frozen.json", {"frozen": True, "after": "pilot and isolation proof"})
    return results, isolation


def _run_remaining(session_dir: Path, config: BenchmarkConfig) -> dict[str, object] | None:
    pilot = _read_json(session_dir / "pilot.json")
    if not isinstance(pilot, dict):
        raise BenchmarkInfrastructureError("--all requires a completed --pilot in the session")
    if not (pilot.get("both_runs_finished") and pilot.get("both_runs_valid")):
        raise BenchmarkInfrastructureError("pilot did not satisfy the valid-run verification gate")
    if not (session_dir / "harness-frozen.json").is_file():
        raise BenchmarkInfrastructureError("harness is not frozen after pilot verification")
    isolation = _read_json(session_dir / "verification" / "isolation" / "result.json")
    if not isinstance(isolation, dict) or isolation.get("passed") is not True:
        raise BenchmarkInfrastructureError("isolation proof is missing or failed")
    randomizer = random.Random(config.seed)
    tasks = list(FIXTURES)
    tasks = [fixture for fixture in tasks if fixture.task_id != PILOT_FIXTURE.task_id]
    randomizer.shuffle(["native", "relay"])  # consume the same pilot shuffle deterministically.
    consecutive_infra = 0
    for fixture in tasks:
        order = ["native", "relay"]
        randomizer.shuffle(order)
        results = _run_pair(session_dir, fixture, config, order=order, seed=config.seed)
        if any(result.get("status") == "INFRA_ERROR" for result in results):
            consecutive_infra += 1
        else:
            consecutive_infra = 0
        if consecutive_infra >= 2:
            _write_json(
                session_dir / "stopped.json",
                {
                    "reason": "two consecutive infrastructure failures",
                    "completed_tasks": [result.get("task_id") for result in _load_results(session_dir)],
                },
            )
            break
    return isolation


def _infer_session_status(session_dir: Path) -> str:
    stopped = _read_json(session_dir / "stopped.json")
    if isinstance(stopped, dict) and stopped.get("reason") == "two consecutive infrastructure failures":
        return "BLOCKED"
    results = _load_results(session_dir)
    by_task: dict[str, dict[str, dict[str, object]]] = {}
    for result in results:
        task_id = result.get("task_id")
        arm = result.get("arm")
        if isinstance(task_id, str) and isinstance(arm, str):
            by_task.setdefault(task_id, {})[arm] = result
    if all(
        _result_is_valid(by_task.get(fixture.task_id, {}).get("native", {}))
        and _result_is_valid(by_task.get(fixture.task_id, {}).get("relay", {}))
        for fixture in FIXTURES
    ):
        return "COMPLETE"
    return "PARTIAL"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pilot", action="store_true", help="run only the first verification pair")
    mode.add_argument("--all", action="store_true", help="run the six-pair suite after pilot verification")
    mode.add_argument("--summary", action="store_true", help="regenerate a session summary")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default=DEFAULT_EFFORT)
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--context-window", type=int, default=DEFAULT_CONTEXT_WINDOW)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--seed", type=int, default=20260828)
    args = parser.parse_args(argv)
    config = _parse_config(args)

    if args.summary:
        if args.session_dir is None:
            parser.error("--summary requires --session-dir")
        summary_session = args.session_dir.expanduser().resolve()
        report = _summary(
            summary_session,
            status=_infer_session_status(summary_session),
            isolation=_read_json(summary_session / "verification" / "isolation" / "result.json")
            if (summary_session / "verification" / "isolation" / "result.json").is_file()
            else None,
        )
        print(report)
        return 0

    session_dir = _make_session(config, args.session_dir)
    print(f"session: {session_dir}", flush=True)
    isolation: dict[str, object] | None = None
    try:
        if args.pilot:
            _run_pilot(session_dir, config)
            report = _summary(
                session_dir,
                status=_infer_session_status(session_dir),
                isolation=_read_json(session_dir / "verification" / "isolation" / "result.json") if (session_dir / "verification" / "isolation" / "result.json").is_file() else None,
            )
            print(report)
            return 0
        if (
            (session_dir / "pilot.json").is_file()
            and (session_dir / "harness-frozen.json").is_file()
            and (session_dir / "verification" / "isolation" / "result.json").is_file()
        ):
            isolation_value = _read_json(session_dir / "verification" / "isolation" / "result.json")
            if isinstance(isolation_value, dict):
                isolation = isolation_value
        else:
            _, isolation = _run_pilot(session_dir, config)
        _run_remaining(session_dir, config)
    except BenchmarkInfrastructureError as error:
        print(f"benchmark stopped: {error}", file=sys.stderr)
        report = _summary(session_dir, status=_infer_session_status(session_dir), isolation=isolation)
        print(report)
        return 2
    report = _summary(session_dir, status=_infer_session_status(session_dir), isolation=isolation)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
