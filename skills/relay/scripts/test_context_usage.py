#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("context_handoff.py")
WRITER = Path(__file__).with_name("write_handoff.py")
SPEC = importlib.util.spec_from_file_location("relay_context_usage_test", SCRIPT)
assert SPEC and SPEC.loader
context_handoff = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = context_handoff
SPEC.loader.exec_module(context_handoff)


def token_count(last_input: int, window: int, total_input: int) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": last_input},
                    "model_context_window": window,
                    "total_token_usage": {"input_tokens": total_input},
                },
            },
        },
        separators=(",", ":"),
    )


class ContextUsageTests(unittest.TestCase):
    def seed_ready_state(self, repo: Path, session_id: str) -> None:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        result = subprocess.run(
            [
                sys.executable,
                str(WRITER),
                "--repo",
                str(repo),
                "--session-id",
                session_id,
                "--revision",
                "1",
                "--update-active-task-only",
                "--objective",
                "Prove the PreToolUse threshold",
                "--goal-objective",
                "Relay threshold Goal Mode objective",
                "--active-task",
                "Emit one clean-task launch envelope",
                "--phase",
                "validation",
                "--status",
                "ready",
                "--completion-criteria",
                "Threshold boundaries are exact",
                "--remaining-work",
                "Inspect the hook response",
                "--constraints",
                "Do not fork the source task",
                "--authoritative-files",
                "skills/relay/scripts/context_handoff.py",
                "--resume-validation-command",
                "python3 -V",
                "--resume-validation-expected",
                "exit 0",
                "--next-action",
                "Inspect the PreToolUse hook response",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_uses_latest_rendered_input_over_cumulative_spend(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "rollout.jsonl"
            transcript.write_text(
                "\n".join(
                    (
                        token_count(20, 100, 200),
                        token_count(30, 100, 230),
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            # When
            ratio = context_handoff.extract_context_used(
                {"transcript_path": str(transcript)}
            )

            # Then
            self.assertEqual(ratio, 0.30)

    def test_reads_only_a_bounded_tail_near_transcript_end(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "rollout.jsonl"
            transcript.write_text(
                json.dumps({"type": "noise", "payload": "x" * 300_000})
                + "\n"
                + token_count(31, 100, 900_000)
                + "\n",
                encoding="utf-8",
            )

            # When
            ratio = context_handoff.extract_context_used(
                {"transcript_path": str(transcript)}
            )

            # Then
            self.assertEqual(ratio, 0.31)

    def test_fails_open_for_missing_malformed_or_changed_usage(self) -> None:
        cases = {
            "missing_path": {},
            "missing_file": {"transcript_path": "/does/not/exist.jsonl"},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            malformed = root / "malformed.jsonl"
            malformed.write_text("{not-json}\n", encoding="utf-8")
            changed = root / "changed.jsonl"
            changed.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "current_usage": {"input_tokens": 30},
                                "effective_context_window": 100,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cases["malformed"] = {"transcript_path": str(malformed)}
            cases["changed_schema"] = {"transcript_path": str(changed)}

            for label, payload in cases.items():
                with self.subTest(label=label):
                    self.assertIsNone(context_handoff.extract_context_used(payload))

    def test_direct_hook_telemetry_precedes_transcript_fallback(self) -> None:
        # Given
        with tempfile.TemporaryDirectory() as temp:
            transcript = Path(temp) / "rollout.jsonl"
            transcript.write_text(token_count(90, 100, 90) + "\n", encoding="utf-8")

            # When
            ratio = context_handoff.extract_context_used(
                {
                    "context_usage_percent": 20,
                    "transcript_path": str(transcript),
                }
            )

            # Then
            self.assertEqual(ratio, 0.20)

    def test_pretool_threshold_is_exact_and_proactive(self) -> None:
        for label, percent, expected_launch in (
            ("below", 29.9, False),
            ("exact", 30, True),
            ("above", 30.1, True),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                # Given
                repo = Path(temp)
                session_id = f"pretool-{label}"
                self.seed_ready_state(repo, session_id)
                payload = json.dumps(
                    {
                        "session_id": session_id,
                        "context_usage_percent": percent,
                        "tool_name": "Read",
                        "tool_input": {},
                    }
                )

                # When
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--repo",
                        str(repo),
                        "--stdin-json",
                        "--trigger",
                        "threshold",
                        "--official-hook-event",
                        "PreToolUse",
                    ],
                    cwd=repo,
                    env=dict(os.environ),
                    input=payload,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                # Then
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                response = json.loads(result.stdout)
                specific = response.get("hookSpecificOutput")
                launch = (
                    isinstance(specific, dict)
                    and isinstance(specific.get("additionalContext"), str)
                )
                self.assertIs(launch, expected_launch)
                self.assertEqual(
                    len(list((repo / ".omx/state/relay").rglob("*-handoff.md"))),
                    int(expected_launch),
                )
                if expected_launch:
                    active_files = list(
                        (repo / ".omx/state/relay").rglob(".active-task.json")
                    )
                    self.assertEqual(len(active_files), 1)
                    active = json.loads(active_files[0].read_text(encoding="utf-8"))
                    expected_identity = (
                        "goal:sha256:"
                        + hashlib.sha256(
                            b"Relay threshold Goal Mode objective"
                        ).hexdigest()
                    )
                    self.assertEqual(active["goal_identity"], expected_identity)

    def test_inflight_transfer_blocks_a_new_revision_after_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            session_id = "pretool-inflight"
            self.seed_ready_state(repo, session_id)
            payload = json.dumps(
                {
                    "session_id": session_id,
                    "context_usage_percent": 31,
                    "tool_name": "Read",
                    "tool_input": {},
                }
            )
            command = [
                sys.executable,
                str(SCRIPT),
                "--repo",
                str(repo),
                "--stdin-json",
                "--trigger",
                "threshold",
                "--dedup-seconds",
                "0",
                "--official-hook-event",
                "PreToolUse",
            ]

            first = subprocess.run(
                command,
                cwd=repo,
                input=payload,
                check=False,
                capture_output=True,
                text=True,
            )
            second = subprocess.run(
                command,
                cwd=repo,
                input=payload,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            self.assertIsInstance(
                json.loads(first.stdout).get("hookSpecificOutput"),
                dict,
            )
            self.assertEqual(json.loads(second.stdout), {})
            transfers = list(
                (repo / ".omx/state/relay").rglob("transfers/*.json")
            )
            capsules = list(
                (repo / ".omx/state/relay").rglob("*-handoff.md")
            )
            self.assertEqual(len(transfers), 1)
            self.assertEqual(len(capsules), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
