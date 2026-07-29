#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
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


class ReadyResult(TypedDict, total=False):
    checkpoint_written: bool
    delivery_emitted: bool
    continuation_prompt: str
    launch_error: str | None
    host_launch: dict[str, str | bool | None] | None


def ready_result() -> ReadyResult:
    return {
        "checkpoint_written": True,
        "delivery_emitted": True,
        "continuation_prompt": "EXACT_BOUNDED_DESTINATION_PROMPT",
        "launch_error": None,
        "host_launch": {
            "acknowledged": True,
            "deduplicated": False,
            "destination_thread_id": "thr_destination",
            "destination_turn_id": "turn_destination",
            "status": "running",
        },
    }


class CodexAppHandoffTests(unittest.TestCase):
    def test_host_transport_does_not_depend_on_model_thread_tools(self) -> None:
        absent = context_handoff.app_capability_guidance({})
        misleading = context_handoff.app_capability_guidance(
            {"available_thread_tools": []},
        )

        self.assertEqual(absent, misleading)
        self.assertTrue(absent["host_managed"])
        self.assertTrue(absent["fresh_thread"])
        self.assertFalse(absent["source_model_action_required"])
        self.assertFalse(absent["desktop_ui_focus_supported"])

    def test_user_prompt_success_returns_small_non_json_context(self) -> None:
        internal = ready_result()

        response = context_handoff.official_hook_response(
            "UserPromptSubmit",
            internal,
        )

        specific = response["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        additional = specific["additionalContext"]
        self.assertIsInstance(additional, str)
        self.assertFalse(str(additional).startswith("{"))

    def test_pretool_success_preserves_event_shape(self) -> None:
        internal = ready_result()

        response = context_handoff.official_hook_response("PreToolUse", internal)

        self.assertNotIn("continue", response)
        specific = response["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertIsInstance(specific["additionalContext"], str)

    def test_failed_launch_returns_manual_fallback_context(self) -> None:
        internal = ready_result()
        internal["delivery_emitted"] = False
        internal["host_launch"] = {
            "acknowledged": True,
            "deduplicated": False,
            "destination_thread_id": "thr_failed",
            "destination_turn_id": "turn_failed",
            "status": "failed",
        }
        internal["launch_error"] = "app-server failed"

        response = context_handoff.official_hook_response(
            "UserPromptSubmit",
            internal,
        )

        specific = response["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        additional = str(specific["additionalContext"])
        self.assertIn("Relay automatic launch failed", additional)
        self.assertIn("EXACT_BOUNDED_DESTINATION_PROMPT", additional)
        self.assertNotIn("Stop heavy work", additional)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
