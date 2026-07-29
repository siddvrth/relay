#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import unittest
from dataclasses import replace

from codex_app_transport_test_support import TransportTestCase, transfer_control


class CodexAppTransportSafetyTests(TransportTestCase):
    def test_approval_request_is_declined_without_blocking_completion(self) -> None:
        os.environ["RELAY_FAKE_MODE"] = "approval"
        transport, config = self.config()

        result = transport.launch(config, detach=False)

        self.assertTrue(result.acknowledged)
        messages = [
            json.loads(line)
            for line in self.request_log.read_text(encoding="utf-8").splitlines()
        ]
        response = next(message for message in messages if message.get("id") == 700)
        self.assertEqual(response["result"], {"decision": "decline"})

    def test_protocol_response_timeout_fails_and_stops_server(self) -> None:
        os.environ["RELAY_FAKE_MODE"] = "hang"
        transport, config = self.config()

        result = transport.launch(
            replace(config, response_timeout=0.1),
            detach=False,
        )

        self.assertFalse(result.acknowledged)
        self.assertIn("protocol_timeout", str(result.error))
        self._assert_fake_server_stopped()

    def test_acknowledgement_timeout_is_unknown_and_stops_worker_group(self) -> None:
        os.environ["RELAY_FAKE_MODE"] = "ack_hang"
        transport, config = self.config()

        result = transport.launch(
            replace(config, response_timeout=60.0),
            detach=True,
            acknowledgement_timeout=0.1,
        )

        self.assertFalse(result.acknowledged)
        self.assertEqual(result.status, "failed")
        self.assertIn("launch_outcome_unknown", str(result.error))
        transfer = transfer_control.status(
            self.repo,
            source_session_id=self.source,
        )
        self.assertEqual(transfer["failure"]["code"], "launch_outcome_unknown")
        self._assert_fake_server_stopped()

    def test_corrupt_delivery_state_fails_closed_before_second_spawn(self) -> None:
        transport, config = self.config()
        first = transport.launch(config, detach=False)
        self.state_path.write_text("{corrupt\n", encoding="utf-8")

        second = transport.launch(config, detach=False)

        self.assertTrue(first.acknowledged)
        self.assertFalse(second.acknowledged)
        self.assertIn("delivery_state_corrupt", str(second.error))
        self.assertEqual(self.create_count.read_text(encoding="utf-8"), "1")

    def _assert_fake_server_stopped(self) -> None:
        if not self.pid_file.exists():
            return
        pid = int(self.pid_file.read_text(encoding="utf-8"))
        for _attempt in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        self.fail(f"fake app-server process {pid} is still alive")


if __name__ == "__main__":
    unittest.main()
