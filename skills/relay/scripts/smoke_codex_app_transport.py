#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import codex_app_protocol
import codex_app_transport
import transfer_control


def main() -> int:
    codex_path = shutil.which("codex")
    if codex_path is None:
        print(json.dumps({"ok": False, "error": "codex binary not found"}))
        return 1
    with tempfile.TemporaryDirectory(prefix="relay-app-smoke-") as temporary:
        repo = Path(temporary).resolve()
        subprocess.run(
            ["git", "init", "-q", str(repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        before = _worktrees(repo)
        source = f"relay-smoke-{uuid.uuid4()}"
        paths = transfer_control.transfer_paths(repo, source)
        paths.session_dir.mkdir(parents=True)
        capsule = paths.session_dir / "smoke-capsule.md"
        capsule.write_text("Relay app-server smoke capsule.\n", encoding="utf-8")
        capsule_sha = hashlib.sha256(capsule.read_bytes()).hexdigest()
        prepared = transfer_control.prepare(
            repo,
            source_session_id=source,
            goal_identity="goal:sha256:" + hashlib.sha256(source.encode()).hexdigest(),
            capsule_path=str(capsule),
            capsule_revision=1,
            capsule_sha256=capsule_sha,
            resume_ready=True,
            next_action="Reply to the smoke prompt.",
            validation_evidence=[],
            resume_validation_command="git status --short",
            resume_validation_expected="exit 0",
            nonce=uuid.uuid4().hex,
        )
        transfer_id = str(prepared["transfer_id"])
        config = codex_app_transport.DeliveryConfig(
            repo=repo,
            cwd=repo,
            capsule_path=capsule,
            continuation_prompt="Reply exactly RELAY_SMOKE_ACK.",
            source_session_id=source,
            delivery_id=transfer_id,
            transfer_id=transfer_id,
            state_path=paths.session_dir / ".delivery.json",
            codex_binary=Path(codex_path),
        )
        first = codex_app_transport.launch(config, detach=False)
        second = codex_app_transport.launch(config, detach=False)
        state = json.loads(config.state_path.read_text(encoding="utf-8"))
        persisted_readable = _read_persisted(
            Path(codex_path),
            str(first.destination_thread_id),
            repo,
        )
        after = _worktrees(repo)
        ok = bool(
            first.acknowledged
            and first.status == "completed"
            and second.deduplicated
            and first.destination_thread_id == second.destination_thread_id
            and state.get("destination_thread_id") == first.destination_thread_id
            and state.get("destination_turn_id") == first.destination_turn_id
            and state.get("destination_readable") is True
            and persisted_readable
            and state.get("cwd") == str(repo)
            and before == after
        )
        print(
            json.dumps(
                {
                    "ok": ok,
                    "destination_thread_id": first.destination_thread_id,
                    "destination_turn_id": first.destination_turn_id,
                    "destination_readable": state.get("destination_readable"),
                    "persisted_readable": persisted_readable,
                    "cwd": state.get("cwd"),
                    "deduplicated": second.deduplicated,
                    "worktrees_unchanged": before == after,
                    "status": state.get("status"),
                    "error": state.get("error"),
                },
                sort_keys=True,
            )
        )
        return 0 if ok else 1


def _worktrees(repo: Path) -> str:
    return subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _read_persisted(codex_binary: Path, thread_id: str, cwd: Path) -> bool:
    process = subprocess.Popen(
        [str(codex_binary), "app-server", "--stdio"],
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    try:
        client = codex_app_protocol.AppServerClient(
            process,
            response_timeout=30.0,
            turn_timeout=30.0,
        )
        client.request(
            "initialize",
            {"clientInfo": codex_app_protocol.CLIENT_INFO},
        )
        client.notify("initialized", {})
        result = client.request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": True,
            },
        )
        thread = result.get("thread")
        return isinstance(thread, dict) and thread.get("id") == thread_id
    finally:
        process.terminate()
        process.wait(timeout=5)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
