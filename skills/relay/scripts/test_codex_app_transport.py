#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import unittest

from codex_app_transport_test_support import TransportTestCase, transfer_control


class CodexAppTransportTests(TransportTestCase):
    def test_success_uses_fresh_thread_protocol_and_persists_real_ids(self) -> None:
        transport, config = self.config()

        result = transport.launch(config, detach=False)

        self.assertTrue(result.acknowledged)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["destination_thread_id"], "thr_fake")
        self.assertEqual(state["destination_turn_id"], "turn_fake")
        self.assertEqual(state["cwd"], str(self.repo))
        self.assertTrue(state["destination_readable"])
        requests = [
            json.loads(line)
            for line in self.request_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [request["method"] for request in requests],
            [
                "initialize",
                "initialized",
                "thread/start",
                "turn/start",
                "thread/read",
            ],
        )
        self.assertEqual(requests[2]["params"], {"cwd": str(self.repo)})
        self.assertEqual(
            requests[3]["params"],
            {
                "threadId": "thr_fake",
                "input": [{"type": "text", "text": self.prompt}],
            },
        )

    def test_same_delivery_is_deduplicated_before_second_spawn(self) -> None:
        transport, config = self.config()
        first = transport.launch(config, detach=False)

        second = transport.launch(config, detach=False)

        self.assertTrue(first.acknowledged)
        self.assertTrue(second.deduplicated)
        self.assertEqual(self.create_count.read_text(encoding="utf-8"), "1")

    def test_protocol_failures_never_report_delivery(self) -> None:
        for mode in ("rpc_error", "turn_error", "malformed", "process_death"):
            with self.subTest(mode=mode):
                os.environ["RELAY_FAKE_MODE"] = mode
                transport, config = self.config()

                result = transport.launch(config, detach=False)

                self.assertFalse(result.acknowledged)
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "failed")
                self.assertIsNotNone(state["error"])
                self.assertFalse(state["delivered"])
                transfer = transfer_control.status(
                    self.repo,
                    source_session_id=self.source,
                )
                self.assertNotEqual(transfer["phase"], "delivered")
                self.state_path.unlink()

    def test_failure_after_acknowledgement_retains_destination_ids(self) -> None:
        os.environ["RELAY_FAKE_MODE"] = "completion_failed"
        transport, config = self.config()

        result = transport.launch(config, detach=False)

        self.assertFalse(result.acknowledged)
        self.assertEqual(result.status, "failed")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["destination_thread_id"], "thr_fake")
        self.assertEqual(state["destination_turn_id"], "turn_fake")
        self.assertFalse(state["delivered"])
        transfer = transfer_control.status(
            self.repo,
            source_session_id=self.source,
        )
        self.assertEqual(
            transfer["failure"]["code"],
            "destination_turn_failed",
        )

    def test_detached_worker_reports_failure_before_acknowledgement(self) -> None:
        os.environ["RELAY_FAKE_MODE"] = "turn_error"
        transport, config = self.config()

        result = transport.launch(config, detach=True)

        self.assertFalse(result.acknowledged)
        self.assertEqual(result.status, "failed")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["acknowledged"])


if __name__ == "__main__":
    unittest.main()
