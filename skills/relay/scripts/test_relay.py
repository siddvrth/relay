from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
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

    def record(message):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, separators=(",", ":")) + "\\n")

    def send(message):
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        if method:
            record(message)
        if "id" not in message:
            continue
        request_id = message["id"]
        params = message.get("params") or {}
        if method == "initialize":
            send({"id": request_id, "result": {}})
        elif method == "thread/goal/get":
            send({"id": request_id, "result": {"goal": {
                "threadId": params["threadId"],
                "objective": os.environ.get("FAKE_CODEX_GOAL", "keep working"),
                "status": "active",
                "tokenBudget": 12345,
            }}})
        elif method == "thread/start":
            with log_path.open("r", encoding="utf-8") as handle:
                starts = sum(1 for item in handle if json.loads(item).get("method") == "thread/start")
            thread_id = "thread-%d" % starts
            send({"id": request_id, "result": {"thread": {"id": thread_id, "cwd": os.getcwd()}}})
        elif method == "thread/goal/set":
            send({"id": request_id, "result": {"goal": params}})
        elif method == "turn/start":
            turn_id = "turn-%s" % thread_id
            send({"id": request_id, "result": {"turn": {"id": turn_id}}})
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

    def tearDown(self) -> None:
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

    def call(self, session: str, *, ratio: float, event: str = "UserPromptSubmit") -> dict:
        return relay.handle_hook(
            repo=self.repo,
            event=event,
            payload={
                "session_id": session,
                "cwd": str(self.repo),
                "prompt": "Continue the release validation",
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

    def test_below_equal_and_above_threshold(self) -> None:
        with self.env():
            self.assertEqual(self.call("below", ratio=0.299), {"continue": True})
            equal = self.call("equal", ratio=0.30)
            above = self.call("above", ratio=0.301)
        self.assertEqual(equal["decision"], "block")
        self.assertEqual(above["decision"], "block")
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

    def test_a_to_b_to_c_uses_distinct_fresh_threads_and_restores_goal(self) -> None:
        with self.env():
            responses = [self.call(session, ratio=0.31) for session in ("A", "B", "C")]
        self.assertTrue(all(response["decision"] == "block" for response in responses))
        entries = self.log_entries()
        starts = [item for item in entries if item.get("method") == "thread/start"]
        self.assertEqual(len(starts), 3)
        goal_sets = [item for item in entries if item.get("method") == "thread/goal/set"]
        self.assertEqual(len(goal_sets), 3)
        self.assertEqual(
            {item["params"]["objective"] for item in goal_sets},
            {"Finish the Relay release"},
        )
        turns = [item for item in entries if item.get("method") == "turn/start"]
        self.assertEqual(len(turns), 3)
        self.assertTrue(all("thread/fork" not in item.get("method", "") for item in entries))
        destinations = [self.state(session)["destination_thread_id"] for session in ("A", "B", "C")]
        self.assertEqual(len(set(destinations)), 3)
        for session in ("A", "B", "C"):
            state = self.state(session)
            self.assertEqual(state["cwd"], str(self.repo.resolve()))
            self.assertEqual(state["objective_source"], "thread/goal/get")
            self.assertNotIn("capsule", json.dumps(state))
            self.assertNotIn("nonce", json.dumps(state))
            self.assertNotIn("revision", json.dumps(state))

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
        missing = self.root / "missing-codex"
        failed = relay.handle_hook(
            repo=self.repo,
            event="UserPromptSubmit",
            payload={"session_id": "retry", "prompt": "retry"},
            context_used=0.31,
            codex_binary=missing,
        )
        self.assertEqual(failed, {"continue": True})
        self.assertEqual(self.state("retry")["status"], "failed")
        with self.env():
            succeeded = self.call("retry", ratio=0.31)
        self.assertEqual(succeeded["decision"], "block")
        self.assertEqual(self.state("retry")["status"], "running")


if __name__ == "__main__":
    unittest.main()
