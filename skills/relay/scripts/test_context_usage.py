from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import context_usage


class ContextUsageTests(unittest.TestCase):
    def test_app_server_notification_uses_latest_total_tokens(self) -> None:
        ratio = context_usage.extract_context_used(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-a",
                    "turnId": "turn-a",
                    "tokenUsage": {
                        "last": {"inputTokens": 250_000, "totalTokens": 90_000},
                        "modelContextWindow": 258_400,
                    },
                },
            }
        )
        expected = (90_000 - context_usage.BASELINE_TOKENS) / (
            258_400 - context_usage.BASELINE_TOKENS
        )
        self.assertAlmostEqual(ratio or -1, expected)

    def test_transcript_uses_total_tokens_not_input_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 220_000,
                                    "total_tokens": 90_000,
                                },
                                "model_context_window": 258_400,
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            ratio = context_usage.extract_context_used({"transcript_path": str(path)})
        expected = (90_000 - 12_000) / (258_400 - 12_000)
        self.assertAlmostEqual(ratio or -1, expected)

    def test_baseline_is_zero_and_window_is_clamped(self) -> None:
        self.assertEqual(
            context_usage.extract_context_used(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "last": {"totalTokens": 1},
                            "modelContextWindow": 258_400,
                        }
                    },
                }
            ),
            0.0,
        )
        self.assertEqual(
            context_usage.extract_context_used(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "last": {"totalTokens": 999_999},
                            "modelContextWindow": 258_400,
                        }
                    },
                }
            ),
            1.0,
        )

    def test_unknown_notification_and_transcript_fail_open(self) -> None:
        self.assertIsNone(context_usage.extract_context_used({"method": "unknown"}))
        self.assertIsNone(
            context_usage.extract_context_used(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {"tokenUsage": {"last": {"totalTokens": 1}}},
                }
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text(
                '{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":1},"model_context_window":258400}}}\n',
                encoding="utf-8",
            )
            self.assertIsNone(
                context_usage.extract_context_used({"transcript_path": str(path)})
            )

    def test_malformed_transcript_and_missing_file_fail_open(self) -> None:
        self.assertIsNone(
            context_usage.extract_context_used(
                {"transcript_path": "/definitely/not/a/transcript.jsonl"}
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl"
            path.write_text("not json\n", encoding="utf-8")
            self.assertIsNone(
                context_usage.extract_context_used({"transcript_path": str(path)})
            )


if __name__ == "__main__":
    unittest.main()
