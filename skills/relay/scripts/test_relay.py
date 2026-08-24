from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import codex_app_transport
import relay
import smoke_codex_app_transport


FAKE_CODEX = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json
    import os
    import pathlib
    import sys

    log_path = pathlib.Path(os.environ["FAKE_CODEX_LOG"])
    thread_id = None
    turn_started = False
    goal_status = "active"
    approval_sent = False
    thread_name = os.environ.get("FAKE_CODEX_TITLE", "Original Relay Goal")

    def record(message):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, separators=(",", ":")) + "\\n")

    def send(message):
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        record(message)
        if "id" not in message:
            continue
        request_id = message["id"]
        params = message.get("params") or {}
        if method == "initialize":
            send({"id": request_id, "result": {}})
        elif method == "thread/goal/get":
            if os.environ.get("FAKE_CODEX_GOAL_ERROR") == "1":
                send({"id": request_id, "error": {"code": -32000, "message": "goal unavailable"}})
            else:
                send({"id": request_id, "result": {"goal": {
                    "threadId": params["threadId"],
                    "objective": os.environ.get("FAKE_CODEX_GOAL", "keep working"),
                    "status": (
                        "complete"
                        if turn_started
                        and os.environ.get("FAKE_CODEX_REQUEST_APPROVAL") != "1"
                        and os.environ.get("FAKE_CODEX_KEEP_GOAL_ACTIVE") != "1"
                        else goal_status
                    ),
                    "tokenBudget": 12345,
                }}})
        elif method == "thread/start":
            with log_path.open("r", encoding="utf-8") as handle:
                starts = sum(1 for item in handle if json.loads(item).get("method") == "thread/start")
            thread_id = os.environ.get("FAKE_CODEX_THREAD_ID", "thread-%d" % starts)
            send({"id": request_id, "result": {
                "thread": {"id": thread_id, "cwd": os.getcwd()},
                "approvalPolicy": params.get("approvalPolicy"),
                "approvalsReviewer": params.get("approvalsReviewer"),
                "model": params.get("model"),
                "sandbox": params.get("sandboxPolicy"),
            }})
        elif method == "thread/goal/set":
            goal_status = params.get("status", goal_status)
            send({"id": request_id, "result": {"goal": {
                "threadId": params.get("threadId"),
                "objective": params.get(
                    "objective", os.environ.get("FAKE_CODEX_GOAL", "keep working")
                ),
                "status": goal_status,
                "tokenBudget": params.get("tokenBudget", 12345),
            }}})
            if turn_started and os.environ.get("FAKE_CODEX_REQUEST_APPROVAL") == "1" and not approval_sent:
                approval_sent = True
                send({"id": 999, "method": "item/fileChange/requestApproval", "params": {
                    "threadId": thread_id, "turnId": "turn-%s" % thread_id, "itemId": "file-change-1"
                }})
        elif method == "thread/name/set":
            thread_name = params.get("name", thread_name)
            send({"id": request_id, "result": {}})
        elif method == "turn/start":
            turn_started = True
            turn_id = "turn-%s" % thread_id
            send({"id": request_id, "result": {"turn": {"id": turn_id}}})
            send({"method": "turn/started", "params": {"turn": {"id": turn_id}}})
            marker = os.environ.get("FAKE_CODEX_FAIL_AFTER_ACK_ONCE")
            if marker and not pathlib.Path(marker).exists():
                pathlib.Path(marker).write_text("failed", encoding="utf-8")
                send({"method": "turn/completed", "params": {"turn": {"id": turn_id, "status": "failed"}}})
            elif os.environ.get("FAKE_CODEX_REQUEST_APPROVAL") == "1":
                send({"id": 999, "method": "item/fileChange/requestApproval", "params": {
                    "threadId": thread_id, "turnId": turn_id, "itemId": "file-change-1"
                }})
            else:
                send({"method": "turn/completed", "params": {"turn": {"id": turn_id, "status": "completed"}}})
        elif method == "thread/read":
            send({"id": request_id, "result": {"thread": {
                "id": params["threadId"],
                "cwd": os.getcwd(),
                "name": thread_name,
                "preview": os.environ.get("FAKE_CODEX_TITLE", "Original Relay Goal"),
            }}})
        else:
            send({"id": request_id, "error": {"code": -32601, "message": method}})
    """
).strip() + "\n"


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.fake_codex.chmod(
            self.fake_codex.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        self.log = self.root / "codex.log"
        self.transcript = self.root / "source.jsonl"
        self.write_transcript()

    def tearDown(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            running = False
            for path in self.repo.glob(".omx/state/relay/*.outcome.json"):
                try:
                    running = json.loads(path.read_text(encoding="utf-8")).get("status") == "running"
                except (OSError, json.JSONDecodeError):
                    running = True
                if running:
                    break
            if not running:
                break
            time.sleep(0.02)
        self.temporary.cleanup()

    def env(self):
        return mock.patch.dict(
            os.environ,
            {
                "FAKE_CODEX_LOG": str(self.log),
                "FAKE_CODEX_GOAL": "Finish the Relay release",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "",
                "RELAY_APP_SERVER_RESPONSE_TIMEOUT": "5",
                "RELAY_APP_SERVER_TURN_TIMEOUT": "5",
            },
            clear=False,
        )

    def write_transcript(self, *, include_settings: bool = True) -> None:
        records = []
        if include_settings:
            records.append(
                {
                    "type": "turn_context",
                    "payload": {
                        "model": "gpt-5.6-luna",
                        "effort": "max",
                        "personality": "pragmatic",
                        "approval_policy": "never",
                        "approvals_reviewer": "auto_review",
                        "sandbox_policy": {
                            "type": "workspace-write",
                            "writable_roots": [str(self.repo)],
                            "network_access": False,
                        },
                        "permission_profile": {"type": "disabled"},
                        "collaboration_mode": {
                            "mode": "default",
                            "settings": {
                                "model": "gpt-5.6-luna",
                                "reasoning_effort": "max",
                                "developer_instructions": None,
                            },
                        },
                        "summary": "concise",
                    },
                }
            )
        records.extend(
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Completed the transport probe; source settings are confirmed.",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Constraint: keep the runtime small and do not add a new framework.",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Remaining work: run the installed smoke; targeted tests currently pass.",
                    },
                },
            ]
        )
        self.transcript.write_text(
            "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
        )

    def call(
        self,
        session: str,
        *,
        ratio: float,
        event: str = "UserPromptSubmit",
        turn_id: str | None = None,
        tool_name: str | None = None,
        tool_input: dict[str, object] | None = None,
    ) -> dict:
        payload: dict[str, object] = {
            "session_id": session,
            "cwd": str(self.repo),
            "prompt": "Continue the release validation",
            "transcript_path": str(self.transcript),
        }
        if turn_id is not None:
            payload["turn_id"] = turn_id
        if tool_name is not None:
            payload["tool_name"] = tool_name
        if tool_input is not None:
            payload["tool_input"] = tool_input
        return relay.handle_hook(
            repo=self.repo,
            event=event,
            payload=payload,
            context_used=ratio,
            codex_binary=self.fake_codex,
        )

    def log_entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def state(self, session: str) -> dict:
        path, _ = relay._state_paths(self.repo, session)
        return json.loads(path.read_text(encoding="utf-8"))

    def outcome(self, session: str, *, status: str | None = None) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.state(session)
            path_value = state.get("outcome_path")
            if isinstance(path_value, str):
                path = Path(path_value)
                try:
                    outcome = json.loads(path.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                else:
                    if status is None or outcome.get("status") == status:
                        return outcome
            time.sleep(0.02)
        self.fail(f"timed out waiting for {session} outcome {status}")

    def test_below_equal_and_above_threshold(self) -> None:
        with self.env():
            self.assertEqual(self.call("below", ratio=0.299), {"continue": True})
            equal = self.call("equal", ratio=0.30)
            above = self.call("above", ratio=0.301)
        self.assertEqual(equal["decision"], "block")
        self.assertEqual(above["decision"], "block")
        self.assertIs(equal["continue"], False)
        self.assertEqual(len([x for x in self.log_entries() if x.get("method") == "thread/start"]), 2)

    def test_duplicate_event_is_idempotent_and_quiesces_pretool(self) -> None:
        with self.env():
            first = self.call("same", ratio=0.31)
            second = self.call("same", ratio=0.31)
            pretool = self.call("same", ratio=0.01, event="PreToolUse")
        self.assertEqual(first["decision"], "block")
        self.assertEqual(second["decision"], "block")
        self.assertEqual(pretool["hookSpecificOutput"]["permissionDecision"], "deny")
        starts = [x for x in self.log_entries() if x.get("method") == "thread/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(self.state("same")["status"], "running")

    def test_goal_cancel_control_bypasses_relay_quiescence(self) -> None:
        with self.env():
            first = self.call("cancel", ratio=0.31)
            control = self.call(
                "cancel",
                ratio=0.01,
                event="PreToolUse",
                tool_name="Goal",
                tool_input={"action": "cancel"},
            )
        self.assertEqual(first["decision"], "block")
        self.assertEqual(control, {})
        self.assertNotIn("permissionDecision", control.get("hookSpecificOutput", {}))
        self.assertEqual(
            len([x for x in self.log_entries() if x.get("method") == "thread/start"]),
            1,
        )

    def test_blocked_goal_status_control_bypasses_quiescence(self) -> None:
        with self.env():
            first = self.call("blocked", ratio=0.31)
            control = self.call(
                "blocked",
                ratio=0.31,
                event="PreToolUse",
                tool_name="functions.update_goal",
                tool_input={"status": "blocked"},
            )
        self.assertEqual(first["decision"], "block")
        self.assertEqual(control, {})
        self.assertNotIn("permissionDecision", control.get("hookSpecificOutput", {}))
        self.assertEqual(
            len([x for x in self.log_entries() if x.get("method") == "thread/start"]),
            1,
        )

    def test_destination_goal_control_does_not_kill_current_worker(self) -> None:
        with self.env():
            self.call("control-source", ratio=0.31)
            destination = self.state("control-source")["destination_thread_id"]
            control = self.call(
                destination,
                ratio=0.31,
                event="PreToolUse",
                tool_name="functions.update_goal",
                tool_input={"status": "blocked"},
            )
        self.assertEqual(control, {})
        self.assertEqual(self.state("control-source")["status"], "running")

    def test_ordinary_shell_text_is_not_a_control_bypass(self) -> None:
        self.assertFalse(
            relay._is_control_operation(
                {"tool_name": "Bash", "tool_input": {"command": "echo complete"}}
            )
        )
        self.assertTrue(
            relay._is_control_operation(
                {"tool_name": "Bash", "tool_input": {"command": "shutdown"}}
            )
        )

    def test_repeated_no_progress_circuit_breaker_stops_chain(self) -> None:
        with self.env(), mock.patch.dict(
            os.environ,
            {"RELAY_NO_PROGRESS_LIMIT": "2"},
            clear=False,
        ):
            self.call("A", ratio=0.31)
            self.outcome("A", status="completed")
            b = self.state("A")["destination_thread_id"]
            self.call(b, ratio=0.31)
            self.outcome(b, status="completed")
            c = self.state(b)["destination_thread_id"]
            stopped = self.call(c, ratio=0.31)
            repeated = self.call(c, ratio=0.31)
            legacy_source = self.call("A", ratio=0.31)
        self.assertEqual(stopped, {"continue": True})
        self.assertEqual(repeated, {"continue": True})
        self.assertEqual(legacy_source, {"continue": True})
        circuit = self.state(c)
        self.assertEqual(circuit["status"], "circuit_breaker")
        self.assertEqual(circuit["circuit_breaker"], "repeated_no_progress")
        self.assertEqual(circuit["no_progress_count"], 2)
        chain_path, _ = relay._chain_paths(self.repo, circuit["relay_chain_id"])
        chain_state = json.loads(chain_path.read_text(encoding="utf-8"))
        self.assertEqual(chain_state["status"], "circuit_breaker")
        self.assertEqual(
            len([x for x in self.log_entries() if x.get("method") == "thread/start"]),
            2,
        )

    def test_reused_destination_id_fails_open_without_quiescing_source(self) -> None:
        with self.env(), mock.patch.dict(
            os.environ,
            {"FAKE_CODEX_THREAD_ID": "same-thread"},
            clear=False,
        ):
            response = self.call("same-thread", ratio=0.31)
        self.assertEqual(response, {"continue": True})
        state = self.state("same-thread")
        self.assertEqual(state["status"], "failed")
        self.assertIn("destination_not_fresh", state["error"])
        self.assertNotIn("destination_thread_id", state)

    def test_actual_destination_b_relay_becomes_actual_destination_c(self) -> None:
        with self.env():
            first = self.call("A", ratio=0.31, turn_id="turn-A")
            self.outcome("A", status="completed")
            b = self.state("A")["destination_thread_id"]
            second = self.call(b, ratio=0.31, turn_id="turn-B")
            self.outcome(b, status="completed")
            c = self.state(b)["destination_thread_id"]
        self.assertEqual(first["decision"], "block")
        self.assertEqual(second["decision"], "block")
        self.assertNotEqual(b, c)
        entries = self.log_entries()
        starts = [item for item in entries if item.get("method") == "thread/start"]
        self.assertEqual(len(starts), 2)
        goal_sets = [item for item in entries if item.get("method") == "thread/goal/set"]
        self.assertEqual(len(goal_sets), 2)
        self.assertEqual(
            {item["params"]["objective"] for item in goal_sets},
            {"Finish the Relay release"},
        )
        self.assertEqual(
            [item["params"]["status"] for item in goal_sets],
            ["active", "active"],
        )
        turns = [item for item in entries if item.get("method") == "turn/start"]
        self.assertEqual(len(turns), 2)
        names = [item for item in entries if item.get("method") == "thread/name/set"]
        self.assertEqual(
            [item["params"]["name"] for item in names],
            ["Relay II: Original Relay Goal", "Relay III: Original Relay Goal"],
        )
        methods = [item.get("method") for item in entries]
        self.assertLess(methods.index("thread/goal/set"), methods.index("turn/start"))
        self.assertTrue(all("thread/fork" not in item.get("method", "") for item in entries))
        for session in ("A", b):
            state = self.state(session)
            self.assertEqual(state["cwd"], str(self.repo.resolve()))
            self.assertEqual(state["objective_source"], "thread/goal/get")
            self.assertNotIn("capsule", json.dumps(state))
            self.assertNotIn("nonce", json.dumps(state))
            self.assertNotIn("revision", json.dumps(state))
        self.assertEqual(self.state("A")["relay_chain_id"], self.state(b)["relay_chain_id"])
        self.assertEqual(self.state("A")["root_thread_id"], "A")
        self.assertIsNone(self.state("A")["parent_thread_id"])
        self.assertEqual(self.state("A")["destination_thread_id"], b)
        self.assertEqual(self.state(b)["root_thread_id"], "A")
        self.assertEqual(self.state(b)["parent_thread_id"], "A")
        self.assertEqual(self.state(b)["destination_thread_id"], c)
        self.assertEqual(self.state("A")["destination_relay_sequence"], 2)
        self.assertEqual(self.state(b)["destination_relay_sequence"], 3)

    def test_desktop_acknowledgement_requires_exact_visible_thread_proof(self) -> None:
        presenter = self.root / "desktop-presenter"
        presenter.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            "from pathlib import Path\n"
            "payload = json.load(__import__('sys').stdin)\n"
            "Path(os.environ['RELAY_DESKTOP_ACK_PATH']).write_text(json.dumps({\n"
            "  'presented': True, 'selected_thread_id': payload['thread_id'],\n"
            "  'thread_id': payload['thread_id'], 'turn_id': payload['turn_id'],\n"
            "  'chain_id': payload['chain_id'], 'relay_sequence': payload['relay_sequence'],\n"
            "  'source_thread_id': payload['source_thread_id']}))\n",
            encoding="utf-8",
        )
        presenter.chmod(presenter.stat().st_mode | stat.S_IXUSR)
        with self.env(), mock.patch.dict(
            os.environ,
            {
                "RELAY_DESKTOP_HANDOFF": "1",
                "RELAY_DESKTOP_PRESENTATION_COMMAND": str(presenter),
                "RELAY_DESKTOP_PRESENTATION_TIMEOUT": "2",
            },
            clear=False,
        ):
            response = self.call("desktop", ratio=0.31)
            outcome = self.outcome("desktop", status="completed")
        self.assertEqual(response["decision"], "block")
        self.assertTrue(outcome["presentation_verified"])
        state = self.state("desktop")
        self.assertEqual(state["presentation_mode"], "desktop")
        self.assertEqual(state["presentation_status"], "presented")

    def test_desktop_without_host_presentation_proof_fails_open(self) -> None:
        with self.env(), mock.patch.dict(
            os.environ,
            {
                "RELAY_DESKTOP_HANDOFF": "1",
                "RELAY_DESKTOP_PRESENTATION_COMMAND": "",
                "RELAY_DESKTOP_PRESENTATION_ACK": "",
                "RELAY_DESKTOP_PRESENTATION_TIMEOUT": "0.2",
            },
            clear=False,
        ):
            response = self.call("desktop-unverified", ratio=0.31)
            state = self.state("desktop-unverified")
        self.assertEqual(response, {"continue": True})
        self.assertEqual(state["status"], "failed")
        self.assertIn("desktop_focus_unsupported", state["error"])
        self.assertFalse("destination_thread_id" in state)

    def test_desktop_a_to_b_to_c_records_visible_ids_and_names(self) -> None:
        presenter = self.root / "desktop-presenter-chain"
        presenter.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "payload = json.load(sys.stdin)\n"
            "Path(os.environ['RELAY_DESKTOP_ACK_PATH']).write_text(json.dumps({\n"
            "  'presented': True, 'selected_thread_id': payload['thread_id'],\n"
            "  'thread_id': payload['thread_id'], 'turn_id': payload['turn_id'],\n"
            "  'chain_id': payload['chain_id'], 'relay_sequence': payload['relay_sequence'],\n"
            "  'source_thread_id': payload['source_thread_id'],\n"
            "  'thread_name': payload['thread_name']}))\n",
            encoding="utf-8",
        )
        presenter.chmod(presenter.stat().st_mode | stat.S_IXUSR)
        with self.env(), mock.patch.dict(
            os.environ,
            {
                "RELAY_DESKTOP_HANDOFF": "1",
                "RELAY_DESKTOP_PRESENTATION_COMMAND": str(presenter),
                "RELAY_DESKTOP_PRESENTATION_TIMEOUT": "2",
            },
            clear=False,
        ):
            first = self.call("desktop-A", ratio=0.31)
            self.outcome("desktop-A", status="completed")
            b = self.state("desktop-A")["destination_thread_id"]
            second = self.call(b, ratio=0.31)
            self.outcome(b, status="completed")
            c = self.state(b)["destination_thread_id"]
        self.assertEqual(first["decision"], "block")
        self.assertEqual(second["decision"], "block")
        self.assertNotEqual("desktop-A", b)
        self.assertNotEqual(b, c)
        self.assertEqual(self.state("desktop-A")["presentation_status"], "presented")
        self.assertEqual(self.state(b)["presentation_status"], "presented")
        self.assertEqual(
            self.state("desktop-A")["destination_thread_name"],
            "Relay II: Original Relay Goal",
        )
        self.assertEqual(
            self.state(b)["destination_thread_name"],
            "Relay III: Original Relay Goal",
        )
        self.assertEqual(self.state("desktop-A")["relay_chain_id"], self.state(b)["relay_chain_id"])
        self.assertEqual(self.state("desktop-A")["destination_thread_id"], b)
        self.assertEqual(self.state(b)["destination_thread_id"], c)

    def test_forced_failure_circuit_breaker_blocks_goal_and_cleans_chain(self) -> None:
        with self.env(), mock.patch.dict(
            os.environ,
            {"RELAY_FAILURE_LIMIT": "2"},
            clear=False,
        ), mock.patch.object(
            relay,
            "launch",
            return_value=mock.Mock(acknowledged=False, error="forced failure"),
        ):
            first = self.call("forced", ratio=0.31)
            second = self.call("forced", ratio=0.31)
        self.assertEqual(first, {"continue": True})
        self.assertEqual(second, {"continue": True})
        state = self.state("forced")
        self.assertEqual(state["status"], "circuit_breaker")
        self.assertEqual(state["circuit_breaker"], "repeated_handoff_failure")
        self.assertEqual(state["goal_status"], "blocked")
        self.assertEqual(state["handoff_failure_count"], 2)
        self.assertFalse(any(item.get("method") == "thread/start" for item in self.log_entries()))

    def test_session_end_cleans_detached_worker_and_outcome(self) -> None:
        state_path, _ = relay._state_paths(self.repo, "quit")
        outcome_path = state_path.with_suffix(".outcome.json")
        relay._write_state(
            outcome_path,
            {"status": "running", "worker_pid": 4242},
        )
        relay._write_state(
            state_path,
            {
                "status": "running",
                "source_session_id": "quit",
                "worker_pid": 4242,
                "outcome_path": str(outcome_path),
                "relay_chain_id": "relay-cleanup-test",
            },
        )
        with mock.patch.object(relay, "stop_worker_pid", return_value=True) as stop:
            response = relay.handle_hook(
                repo=self.repo,
                event="SessionEnd",
                payload={"session_id": "quit", "cwd": str(self.repo)},
            )
        self.assertEqual(response, {})
        stop.assert_called_once_with(4242, repo=self.repo.resolve())
        self.assertEqual(self.state("quit")["status"], "cancelled")
        self.assertEqual(json.loads(outcome_path.read_text(encoding="utf-8"))["status"], "cancelled")

    def test_session_end_without_id_does_not_clean_unrelated_workers(self) -> None:
        state_path, _ = relay._state_paths(self.repo, "unrelated")
        relay._write_state(
            state_path,
            {
                "status": "running",
                "source_session_id": "unrelated",
                "worker_pid": 4545,
            },
        )
        with mock.patch.object(relay, "stop_worker_pid") as stop:
            response = relay.handle_hook(
                repo=self.repo,
                event="SessionEnd",
                payload={"cwd": str(self.repo)},
            )
        self.assertEqual(response, {})
        stop.assert_not_called()
        self.assertEqual(self.state("unrelated")["status"], "running")

    def test_destination_session_end_cleans_parent_chain_worker(self) -> None:
        state_path, _ = relay._state_paths(self.repo, "source-A")
        outcome_path = state_path.with_suffix(".outcome.json")
        relay._write_state(outcome_path, {"status": "running", "worker_pid": 4343})
        relay._write_state(
            state_path,
            {
                "status": "running",
                "source_session_id": "source-A",
                "destination_thread_id": "destination-B",
                "worker_pid": 4343,
                "outcome_path": str(outcome_path),
                "relay_chain_id": "relay-destination-quit",
            },
        )
        with mock.patch.object(relay, "stop_worker_pid", return_value=True) as stop:
            response = relay.handle_hook(
                repo=self.repo,
                event="SessionEnd",
                payload={"session_id": "destination-B", "cwd": str(self.repo)},
            )
        self.assertEqual(response, {})
        stop.assert_called_once_with(4343, repo=self.repo.resolve())
        self.assertEqual(relay._read_state(state_path)["status"], "cancelled")

    def test_real_worker_process_group_is_killed_on_cleanup(self) -> None:
        fake_server = self.root / "hanging-codex"
        fake_server.write_text(
            "#!/usr/bin/env python3\n"
            "import time\n"
            "for _line in __import__('sys').stdin:\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        fake_server.chmod(fake_server.stat().st_mode | stat.S_IXUSR)
        cleanup_repo = self.repo.resolve()
        request = cleanup_repo / ".omx/state/relay/.request-real-cleanup.json"
        outcome = cleanup_repo / ".omx/state/relay/real-cleanup.outcome.json"
        state_path, _ = relay._state_paths(cleanup_repo, "real-cleanup")
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            json.dumps(
                {
                    "cwd": str(self.repo.resolve()),
                    "continuation_prompt": "continue",
                    "codex_binary": str(fake_server),
                    "goal_objective": "Finish cleanup",
                    "goal_status": "active",
                    "goal_token_budget": None,
                    "settings": {},
                    "outcome_path": str(outcome),
                    "response_timeout": 30,
                    "turn_timeout": 30,
                    "presentation_mode": "headless",
                    "presentation_timeout": 1,
                }
            ),
            encoding="utf-8",
        )
        read_fd, write_fd = os.pipe()
        worker = subprocess.Popen(
            [
                relay.sys.executable,
                str(Path(relay.__file__).with_name("codex_app_transport.py")),
                "--worker-request",
                str(request),
                "--ack-fd",
                str(write_fd),
            ],
            cwd=cleanup_repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
        os.close(write_fd)
        os.close(read_fd)
        relay._write_state(
            state_path,
            {
                "status": "running",
                "source_session_id": "real-cleanup",
                "worker_pid": worker.pid,
                "outcome_path": str(outcome),
            },
        )
        try:
            time.sleep(0.1)
            result = relay.cleanup_workers(cleanup_repo)
            worker.wait(timeout=5)
            self.assertIn(worker.pid, result["cleaned"])
            self.assertEqual(json.loads(outcome.read_text())["status"], "cancelled")
        finally:
            if worker.poll() is None:
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                worker.wait()

    def test_failed_handoff_cleanup_stops_recorded_worker_before_retry(self) -> None:
        state_path, _ = relay._state_paths(self.repo, "failed-cleanup")
        outcome_path = state_path.with_suffix(".outcome.json")
        relay._write_state(outcome_path, {"status": "failed", "error": "turn failed"})
        relay._write_state(
            state_path,
            {
                "status": "running",
                "source_session_id": "failed-cleanup",
                "worker_pid": 4646,
                "outcome_path": str(outcome_path),
                "relay_chain_id": "relay-failed-cleanup",
            },
        )
        with mock.patch.object(relay, "stop_worker_pid", return_value=True) as stop:
            response = self.call("failed-cleanup", ratio=0.01)
        self.assertEqual(response, {"continue": True})
        stop.assert_called_once_with(4646, repo=self.repo.resolve())
        self.assertEqual(self.state("failed-cleanup")["status"], "failed")

    def test_successor_ack_completes_worker_while_destination_goal_stays_active(
        self,
    ) -> None:
        with self.env(), mock.patch.dict(
            os.environ,
            {"FAKE_CODEX_KEEP_GOAL_ACTIVE": "1"},
            clear=False,
        ):
            response = self.call("source-A", ratio=0.31)
            state = self.state("source-A")
            destination_b = state["destination_thread_id"]
            b_state_path, _ = relay._state_paths(self.repo, destination_b)
            relay._write_state(
                b_state_path,
                {
                    "status": "running",
                    "source_session_id": destination_b,
                    "destination_thread_id": "destination-C",
                },
            )
            outcome = self.outcome("source-A", status="completed")

        self.assertEqual(response["decision"], "block")
        self.assertEqual(state["status"], "running")
        self.assertEqual(outcome["thread_id"], destination_b)
        self.assertEqual(outcome["turn_id"], state["destination_turn_id"])
        entries = self.log_entries()
        goal_sets = [
            entry for entry in entries if entry.get("method") == "thread/goal/set"
        ]
        self.assertEqual(
            [entry["params"].get("status") for entry in goal_sets],
            ["active"],
        )
        self.assertFalse(
            any(entry.get("method") == "turn/interrupt" for entry in entries)
        )

        worker_pid = state["worker_pid"]
        self.assertIsInstance(worker_pid, int)

        def worker_group_members() -> list[str]:
            process = subprocess.run(
                ["ps", "-axo", "pgid=,stat=,command="],
                capture_output=True,
                text=True,
                check=False,
            )
            members: list[str] = []
            for line in process.stdout.splitlines():
                fields = line.strip().split(maxsplit=2)
                if len(fields) != 3:
                    continue
                try:
                    pgid = int(fields[0])
                except ValueError:
                    continue
                if pgid == worker_pid and not fields[1].startswith("Z"):
                    members.append(fields[2])
            return members

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            codex_app_transport._pid_exists(worker_pid) or worker_group_members()
        ):
            time.sleep(0.02)
        self.assertFalse(codex_app_transport._pid_exists(worker_pid))
        self.assertEqual(worker_group_members(), [])

    def test_smoke_marker_requires_an_assistant_message(self) -> None:
        marker = "RELAY_SMOKE_C_ACK"
        rollout = self.root / "marker-rollout.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": marker}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "completed assistant message"):
            smoke_codex_app_transport._wait_agent_message(
                rollout,
                marker,
                timeout=0.05,
            )

        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": marker},
                    }
                )
                + "\n"
            )
        with self.assertRaisesRegex(RuntimeError, "completed assistant message"):
            smoke_codex_app_transport._wait_agent_message(
                rollout,
                marker,
                timeout=0.05,
            )

        with rollout.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": marker,
                        },
                    }
                )
                + "\n"
            )
        smoke_codex_app_transport._wait_agent_message(
            rollout,
            marker,
            timeout=0.1,
        )

    def test_circuit_breaker_blocks_before_cleaning_other_chain_workers(self) -> None:
        events: list[str] = []
        chain = {
            "chain_id": "relay-order-test",
            "root_thread_id": "A",
            "parent_thread_id": "B",
            "source_sequence": 3,
            "original_title": "Original Relay Goal",
        }
        with mock.patch.object(
            relay,
            "set_thread_goal_status",
            side_effect=lambda **_: events.append("blocked"),
        ), mock.patch.object(
            relay,
            "cleanup_workers",
            side_effect=lambda *args, **kwargs: events.append("cleanup"),
        ) as cleanup:
            relay._trip_circuit_breaker(
                repo=self.repo,
                state={"cwd": str(self.repo)},
                session_id="C",
                objective="Finish the Relay release",
                chain=chain,
                files="none reported",
                ratio=0.31,
                threshold=0.30,
                progress_fingerprint="progress",
                no_progress_count=3,
                failure_count=0,
                failure="repeated observations with no progress marker change",
                codex_binary=self.fake_codex,
            )
        self.assertEqual(events, ["blocked", "cleanup"])
        cleanup.assert_called_once_with(
            self.repo,
            chain_id="relay-order-test",
            protect_destination_session_id="C",
        )
        self.assertEqual(self.state("C")["goal_status"], "blocked")

    def test_chain_cleanup_protects_current_destination_worker(self) -> None:
        chain_id = "relay-protected-worker-test"

        def write_worker_state(
            session_id: str,
            destination_thread_id: str,
            worker_pid: int,
        ) -> tuple[Path, Path]:
            state_path, _ = relay._state_paths(self.repo, session_id)
            outcome_path = state_path.with_suffix(".outcome.json")
            relay._write_state(
                outcome_path,
                {"status": "running", "worker_pid": worker_pid},
            )
            relay._write_state(
                state_path,
                {
                    "status": "running",
                    "source_session_id": session_id,
                    "destination_thread_id": destination_thread_id,
                    "worker_pid": worker_pid,
                    "outcome_path": str(outcome_path),
                    "relay_chain_id": chain_id,
                },
            )
            return state_path, outcome_path

        protected_state, protected_outcome = write_worker_state("B", "C", 111)
        child_state, child_outcome = write_worker_state("C", "D", 222)
        with mock.patch.object(relay, "stop_worker_pid", return_value=True) as stop:
            result = relay.cleanup_workers(
                self.repo,
                chain_id=chain_id,
                protect_destination_session_id="C",
            )
        self.assertEqual(result, {"cleaned": [222], "skipped": []})
        stop.assert_called_once_with(222, repo=self.repo)
        self.assertEqual(relay._read_state(protected_state)["status"], "running")
        self.assertEqual(relay._read_state(protected_outcome)["status"], "running")
        self.assertEqual(relay._read_state(child_state)["status"], "cancelled")
        self.assertEqual(relay._read_state(child_outcome)["status"], "cancelled")

    def test_preserves_effective_execution_settings_on_first_turn(self) -> None:
        with self.env():
            self.call("settings", ratio=0.31)
            self.outcome("settings", status="completed")
        starts = [item for item in self.log_entries() if item.get("method") == "thread/start"]
        turns = [item for item in self.log_entries() if item.get("method") == "turn/start"]
        self.assertEqual(starts[-1]["params"]["model"], "gpt-5.6-luna")
        self.assertEqual(starts[-1]["params"]["approvalPolicy"], "never")
        self.assertEqual(starts[-1]["params"]["sandbox"], "workspace-write")
        self.assertEqual(starts[-1]["params"]["approvalsReviewer"], "auto_review")
        self.assertEqual(starts[-1]["params"]["personality"], "pragmatic")
        self.assertEqual(turns[-1]["params"]["effort"], "max")
        self.assertEqual(turns[-1]["params"]["sandboxPolicy"]["type"], "workspaceWrite")
        self.assertEqual(
            turns[-1]["params"]["collaborationMode"]["mode"],
            "default",
        )

    def test_continuation_keeps_recent_progress_constraints_and_validation(self) -> None:
        with self.env():
            self.call("continuation", ratio=0.31)
            self.outcome("continuation", status="completed")
        turn = [item for item in self.log_entries() if item.get("method") == "turn/start"][-1]
        prompt = turn["params"]["input"][0]["text"]
        self.assertIn("Completed the transport probe", prompt)
        self.assertIn("keep the runtime small", prompt)
        self.assertIn("targeted tests currently pass", prompt)

    def test_duplicate_concurrent_events_create_one_destination(self) -> None:
        with self.env(), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.call, "race", ratio=0.31) for _ in range(2)]
            responses = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(response.get("decision") == "block" for response in responses))
        self.assertEqual(
            len([x for x in self.log_entries() if x.get("method") == "thread/start"]),
            1,
        )

    def test_unavailable_telemetry_and_malformed_state_fail_open(self) -> None:
        with self.env():
            unavailable = relay.handle_hook(
                repo=self.repo,
                event="UserPromptSubmit",
                payload={"session_id": "unknown", "transcript_path": str(self.root / "missing")},
                codex_binary=self.fake_codex,
            )
            self.assertEqual(unavailable, {"continue": True})
            path, _ = relay._state_paths(self.repo, "broken")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{broken", encoding="utf-8")
            malformed = self.call("broken", ratio=0.31)
        self.assertEqual(malformed["decision"], "block")

    def test_failed_launch_can_retry_on_a_later_eligible_event(self) -> None:
        with self.env(), mock.patch.object(
            relay,
            "launch",
            return_value=mock.Mock(acknowledged=False, error="launch failed"),
        ):
            failed = self.call("retry", ratio=0.31)
        self.assertEqual(failed, {"continue": True})
        self.assertEqual(self.state("retry")["status"], "failed")
        with self.env():
            succeeded = self.call("retry", ratio=0.31)
        self.assertEqual(succeeded["decision"], "block")
        self.assertEqual(self.state("retry")["status"], "running")

    def test_post_ack_turn_failure_retries_instead_of_stranding_source(self) -> None:
        marker = self.root / "failed-once"
        with self.env(), mock.patch.dict(
            os.environ,
            {"FAKE_CODEX_FAIL_AFTER_ACK_ONCE": str(marker)},
            clear=False,
        ):
            first = self.call("post-ack", ratio=0.31)
            self.assertEqual(first["decision"], "block")
            self.outcome("post-ack", status="failed")
            failed_destination = self.state("post-ack")["destination_thread_id"]
            second = self.call("post-ack", ratio=0.31)
            self.assertEqual(second["decision"], "block")
            self.outcome("post-ack", status="completed")
        self.assertNotEqual(
            failed_destination,
            self.state("post-ack")["destination_thread_id"],
        )
        self.assertEqual(
            len([x for x in self.log_entries() if x.get("method") == "thread/start"]),
            2,
        )

    def test_goal_or_settings_unavailable_fail_open_for_later_retry(self) -> None:
        with self.env(), mock.patch.dict(
            os.environ,
            {"FAKE_CODEX_GOAL_ERROR": "1"},
            clear=False,
        ):
            self.assertEqual(self.call("goal-error", ratio=0.31), {"continue": True})
        self.write_transcript(include_settings=False)
        with self.env():
            self.assertEqual(self.call("settings-error", ratio=0.31), {"continue": True})
        self.assertFalse(any(x.get("method") == "thread/start" for x in self.log_entries()))

    def test_approval_request_is_not_automatically_declined(self) -> None:
        with self.env(), mock.patch.dict(
            os.environ,
            {"FAKE_CODEX_REQUEST_APPROVAL": "1"},
            clear=False,
        ):
            response = self.call("approval", ratio=0.31)
            outcome = self.outcome("approval", status="failed")
        self.assertEqual(response["decision"], "block")
        self.assertIn("approval", outcome["error"])
        self.assertFalse(any(item.get("id") == 999 for item in self.log_entries()))


if __name__ == "__main__":
    unittest.main()
