#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
TRANSPORT_PATH = SCRIPTS / "codex_app_transport.py"
TRANSFER_PATH = SCRIPTS / "transfer_control.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


transfer_control = load_module(
    "relay_transport_transfer_control_test",
    TRANSFER_PATH,
)

FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

mode = os.environ.get("RELAY_FAKE_MODE", "success")
request_log = Path(os.environ["RELAY_FAKE_REQUEST_LOG"])
create_count = Path(os.environ["RELAY_FAKE_CREATE_COUNT"])
Path(os.environ["RELAY_FAKE_PID_FILE"]).write_text(str(os.getpid()), encoding="utf-8")

def send(payload):
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    request = json.loads(line)
    with request_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, separators=(",", ":")) + "\n")
    method = request.get("method")
    if method == "initialize":
        send({"method": "server/ready", "params": {}})
        send({"id": 999, "result": {"ignored": True}})
        send({"id": request["id"], "result": {"userAgent": "relay-fake"}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        if mode in {"hang", "ack_hang"}:
            continue
        if mode == "process_death":
            raise SystemExit(7)
        if mode == "malformed":
            sys.stdout.write("{not-json\n")
            sys.stdout.flush()
            continue
        if mode == "rpc_error":
            send({"id": request["id"], "error": {"code": -32000, "message": "no thread"}})
            continue
        count = int(create_count.read_text(encoding="utf-8") or "0") + 1
        create_count.write_text(str(count), encoding="utf-8")
        send({"method": "thread/started", "params": {"thread": {"id": "thr_fake"}}})
        send({"id": request["id"], "result": {"thread": {"id": "thr_fake", "cwd": request["params"]["cwd"]}}})
    elif method == "turn/start":
        if mode == "turn_error":
            send({"id": request["id"], "error": {"code": -32001, "message": "no turn"}})
            continue
        send({"method": "turn/started", "params": {"turn": {"id": "turn_fake"}}})
        if mode == "approval":
            send({
                "id": 700,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thr_fake", "turnId": "turn_fake", "itemId": "item_fake"},
            })
        send({"method": "item/completed", "params": {"item": {"id": "item_fake"}}})
        status = "failed" if mode == "completion_failed" else "completed"
        send({"method": "turn/completed", "params": {"turn": {"id": "turn_fake", "status": status}}})
        send({"id": request["id"], "result": {"turn": {"id": "turn_fake", "status": "inProgress"}}})
    elif method == "thread/read":
        send({"id": request["id"], "result": {"thread": {"id": "thr_fake", "cwd": os.getcwd(), "turns": [{"id": "turn_fake"}]}}})
"""


class TransportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name).resolve()
        session_dir = transfer_control.transfer_paths(
            self.repo,
            "source-session",
        ).session_dir
        self.capsule = session_dir / "capsule.md"
        self.capsule.parent.mkdir(parents=True)
        self.capsule.write_text("ready capsule\n", encoding="utf-8")
        self.fake_server = self.repo / "fake-codex"
        self.fake_server.write_text(FAKE_SERVER, encoding="utf-8")
        self.fake_server.chmod(self.fake_server.stat().st_mode | stat.S_IXUSR)
        self.request_log = self.repo / "requests.jsonl"
        self.create_count = self.repo / "create-count"
        self.create_count.write_text("0", encoding="utf-8")
        self.pid_file = self.repo / "fake-server.pid"
        self.state_path = self.capsule.parent / ".delivery.json"
        self.source = "source-session"
        self.delivery_id = "r1-transportdelivery01"
        self.prompt = "Continue from the exact Relay capsule."
        sha = hashlib.sha256(self.capsule.read_bytes()).hexdigest()
        prepared = transfer_control.prepare(
            self.repo,
            source_session_id=self.source,
            goal_identity="goal:sha256:" + "a" * 64,
            capsule_path=str(self.capsule),
            capsule_revision=1,
            capsule_sha256=sha,
            resume_ready=True,
            next_action="Run the focused validation.",
            validation_evidence=[],
            resume_validation_command="git status --short",
            resume_validation_expected="exit 0",
            nonce="transportdelivery0123456789AB",
        )
        self.transfer_id = str(prepared["transfer_id"])
        os.environ["RELAY_FAKE_REQUEST_LOG"] = str(self.request_log)
        os.environ["RELAY_FAKE_CREATE_COUNT"] = str(self.create_count)
        os.environ["RELAY_FAKE_PID_FILE"] = str(self.pid_file)

    def tearDown(self) -> None:
        os.environ.pop("RELAY_FAKE_MODE", None)
        os.environ.pop("RELAY_FAKE_REQUEST_LOG", None)
        os.environ.pop("RELAY_FAKE_CREATE_COUNT", None)
        os.environ.pop("RELAY_FAKE_PID_FILE", None)
        self.temporary.cleanup()

    def config(self):
        transport = load_module(
            "relay_codex_app_transport_test",
            TRANSPORT_PATH,
        )
        return transport, transport.DeliveryConfig(
            repo=self.repo,
            cwd=self.repo,
            capsule_path=self.capsule,
            continuation_prompt=self.prompt,
            source_session_id=self.source,
            delivery_id=self.delivery_id,
            transfer_id=self.transfer_id,
            state_path=self.state_path,
            codex_binary=self.fake_server,
        )
