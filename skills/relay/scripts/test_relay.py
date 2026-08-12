from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import relay


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
                    "status": "complete" if turn_started else goal_status,
                    "tokenBudget": 12345,
                }}})
        elif method == "thread/start":
            with log_path.open("r", encoding="utf-8") as handle:
                starts = sum(1 for item in handle if json.loads(item).get("method") == "thread/start")
            thread_id = "thread-%d" % starts
            send({"id": request_id, "result": {
                "thread": {"id": thread_id, "cwd": os.getcwd()},
                "approvalPolicy": params.get("approvalPolicy"),
                "approvalsReviewer": params.get("approvalsReviewer"),
                "model": params.get("model"),
                "sandbox": params.get("sandboxPolicy"),
            }})
        elif method == "thread/goal/set":
            goal_status = params.get("status", goal_status)
            send({"id": request_id, "result": {"goal": params}})
            if turn_started and os.environ.get("FAKE_CODEX_REQUEST_APPROVAL") == "1" and not approval_sent:
                approval_sent = True
                send({"id": 999, "method": "item/fileChange/requestApproval", "params": {
                    "threadId": thread_id, "turnId": "turn-%s" % thread_id, "itemId": "file-change-1"
                }})
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
            send({"id": request_id, "result": {"thread": {"id": params["threadId"], "cwd": os.getcwd()}}})
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

    def call(self, session: str, *, ratio: float, event: str = "UserPromptSubmit") -> dict:
        return relay.handle_hook(
            repo=self.repo,
            event=event,
            payload={
                "session_id": session,
                "cwd": str(self.repo),
                "prompt": "Continue the release validation",
                "transcript_path": str(self.transcript),
            },
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

    def test_actual_destination_b_relay_becomes_actual_destination_c(self) -> None:
        with self.env():
            first = self.call("A", ratio=0.31)
            self.outcome("A", status="completed")
            b = self.state("A")["destination_thread_id"]
            second = self.call(b, ratio=0.31)
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
        self.assertTrue(all("thread/fork" not in item.get("method", "") for item in entries))
        for session in ("A", b):
            state = self.state(session)
            self.assertEqual(state["cwd"], str(self.repo.resolve()))
            self.assertEqual(state["objective_source"], "thread/goal/get")
            self.assertNotIn("capsule", json.dumps(state))
            self.assertNotIn("nonce", json.dumps(state))
            self.assertNotIn("revision", json.dumps(state))

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
        self.assertEqual(response, {"continue": True})
        self.assertIn("approval", self.state("approval")["error"])
        self.assertFalse(any(item.get("id") == 999 for item in self.log_entries()))


if __name__ == "__main__":
    unittest.main()
