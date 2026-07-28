#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import TypedDict


SCRIPT = Path(__file__).with_name("context_handoff.py")
SPEC = importlib.util.spec_from_file_location("relay_context_app_test", SCRIPT)
assert SPEC and SPEC.loader
context_handoff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = context_handoff
SPEC.loader.exec_module(context_handoff)


class LifecycleAction(TypedDict, total=False):
    phase: str
    command_argv: list[str]
    app_action: str
    commands_argv: list[list[str]]


class ReadyResult(TypedDict):
    checkpoint_written: bool
    delivery_emitted: bool
    continuation_prompt: str
    lifecycle_next_actions: list[LifecycleAction]


class LaunchEnvelope(TypedDict):
    contract: str
    app_action: str
    initial_prompt: str
    lifecycle: list[LifecycleAction]
    destination_id_source: str
    source_stop_gate: str


def ready_result() -> ReadyResult:
    return {
        "checkpoint_written": True,
        "delivery_emitted": True,
        "continuation_prompt": "EXACT_BOUNDED_DESTINATION_PROMPT",
        "lifecycle_next_actions": [
            {
                "phase": "launch_requested",
                "command_argv": ["python3", "transfer_control.py", "launch-requested"],
            },
            {
                "phase": "create_clean_task",
                "app_action": "create_thread",
            },
            {
                "phase": "delivered_and_started",
                "commands_argv": [
                    ["python3", "transfer_control.py", "delivered"],
                    ["python3", "transfer_control.py", "started"],
                ],
            },
        ],
    }


def parse_launch_context(value: str) -> LaunchEnvelope:
    return json.loads(value)


class CodexAppHandoffTests(unittest.TestCase):
    def test_missing_host_tool_telemetry_remains_unknown(self) -> None:
        unknown = context_handoff.app_capability_guidance({})
        unsupported = context_handoff.app_capability_guidance(
            {"available_thread_tools": []},
        )
        supported = context_handoff.app_capability_guidance(
            {"available_thread_tools": ["create_thread"]},
        )

        self.assertIsNone(unknown["create_clean_task_supported"])
        self.assertFalse(unsupported["create_clean_task_supported"])
        self.assertTrue(supported["create_clean_task_supported"])

    def test_lifecycle_uses_one_stable_transport_key_before_and_after_create(self) -> None:
        transfer = {
            "session_id": "source-session",
            "transfer_id": "r1-0123456789abcdef",
        }

        actions = context_handoff.lifecycle_next_actions(
            Path("/tmp/relay-app-test"),
            transfer,
            create_thread_available=True,
        )

        launch_command = actions[0]["command_argv"]
        delivered_command = actions[2]["commands_argv"][0]
        launch_key = launch_command[launch_command.index("--transport-key") + 1]
        delivered_key = delivered_command[
            delivered_command.index("--transport-key") + 1
        ]
        self.assertEqual(launch_key, transfer["transfer_id"])
        self.assertEqual(delivered_key, transfer["transfer_id"])

    def test_user_prompt_ready_handoff_requests_one_clean_app_task(self) -> None:
        internal = ready_result()

        response = context_handoff.official_hook_response(
            "UserPromptSubmit",
            internal,
        )

        envelope = parse_launch_context(
            response["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(envelope["contract"], "relay.codex_app.clean_task.v1")
        self.assertEqual(envelope["app_action"], "create_thread")
        self.assertEqual(
            envelope["initial_prompt"],
            internal["continuation_prompt"],
        )
        self.assertEqual(
            [step["phase"] for step in envelope["lifecycle"]],
            [
                "launch_requested",
                "create_clean_task",
                "delivered_and_started",
            ],
        )
        self.assertEqual(envelope["destination_id_source"], "create_thread.threadId")
        self.assertEqual(envelope["source_stop_gate"], "destination_acknowledged")

    def test_pretool_ready_handoff_requests_one_clean_app_task(self) -> None:
        internal = ready_result()

        response = context_handoff.official_hook_response("PreToolUse", internal)

        self.assertNotIn("continue", response)
        envelope = parse_launch_context(
            response["hookSpecificOutput"]["additionalContext"],
        )
        self.assertEqual(
            response["hookSpecificOutput"]["hookEventName"],
            "PreToolUse",
        )
        self.assertEqual(envelope["app_action"], "create_thread")
        self.assertEqual(
            envelope["initial_prompt"],
            internal["continuation_prompt"],
        )

    def test_precompact_ready_handoff_only_reports_checkpoint(self) -> None:
        internal = ready_result()

        response = context_handoff.official_hook_response("PreCompact", internal)

        self.assertEqual(
            response,
            {
                "continue": True,
                "systemMessage": "Relay state refreshed before compaction.",
            },
        )

    def test_checkpoint_without_delivery_does_not_request_an_app_task(self) -> None:
        internal = ready_result()
        internal["delivery_emitted"] = False

        response = context_handoff.official_hook_response("PreCompact", internal)

        self.assertEqual(
            response,
            {
                "continue": True,
                "systemMessage": "Relay state refreshed before compaction.",
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
