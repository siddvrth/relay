#!/usr/bin/env python3
"""Focused writer/context/hook integration tests for transfer authority."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[2]
WRITER = SCRIPT_DIR / "write_handoff.py"
CONTEXT = SCRIPT_DIR / "context_handoff.py"
TRANSFER = SCRIPT_DIR / "transfer_control.py"
PLUGIN_HOOK = REPO / "hooks" / "checkpoint_and_continue_hook.sh"
CODEX_HOOK = REPO / "codex" / "checkpoint_and_continue_hook.sh"
WORKFLOW_HOOK = REPO / "scripts" / "workflow" / "checkpoint_and_continue_hook.sh"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


transfer = load_module("transfer_control", TRANSFER)
writer = load_module("write_handoff_integration", WRITER)


class TransferIntegrationTests(unittest.TestCase):
    SOURCE = "source-integration"
    DESTINATION = "destination-integration"
    TASK = "destination-task"
    GOAL = "goal:sha256:" + "1" * 64
    NONCE = "integrationnonceabcdefghijklmnop"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        paths = transfer.transfer_paths(self.repo, self.SOURCE)
        paths.session_dir.mkdir(parents=True)
        self.capsule = paths.session_dir / "seed-r1-handoff.md"
        self.capsule.write_text("seed exact ready capsule\n", encoding="utf-8")
        self.sha = hashlib.sha256(self.capsule.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bind_destination(self) -> tuple[str, dict[str, object]]:
        prepared = transfer.prepare(
            self.repo,
            source_session_id=self.SOURCE,
            goal_identity=self.GOAL,
            capsule_path=str(self.capsule),
            capsule_revision=1,
            capsule_sha256=self.sha,
            resume_ready=True,
            nonce=self.NONCE,
        )
        transfer_id = str(prepared["transfer_id"])
        transfer.launch_requested(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
        )
        transfer.delivered(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
            destination_task_id=self.TASK,
        )
        transfer.started(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            destination_session_id=self.DESTINATION,
            destination_task_id=self.TASK,
        )
        exact: dict[str, object] = {
            "source_session_id": self.SOURCE,
            "transfer_id": transfer_id,
            "destination_session_id": self.DESTINATION,
            "destination_task_id": self.TASK,
            "goal_identity": self.GOAL,
            "capsule_path": str(self.capsule),
            "capsule_revision": 1,
            "capsule_sha256": self.sha,
            "nonce": self.NONCE,
        }
        transfer.verify(
            self.repo,
            **exact,
            repository_inspected=True,
            goal_inspected=True,
            exact_next_action="run integration test",
            smallest_validation="focused integration suite",
        )
        return transfer_id, exact

    def acknowledge(self) -> tuple[str, dict[str, object]]:
        transfer_id, exact = self.bind_destination()
        transfer.acknowledge(self.repo, **exact)
        return transfer_id, exact

    @staticmethod
    def tree_snapshot(repo: Path) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for path in repo.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                snapshot[str(path.relative_to(repo))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snapshot

    def run_hook(
        self,
        hook: Path,
        event: str,
        payload: dict[str, object],
        *,
        plugin_root: Path | None = REPO,
    ) -> dict[str, object]:
        env = os.environ.copy()
        env["ROOT"] = str(self.repo)
        if plugin_root is not None:
            env["PLUGIN_ROOT"] = str(plugin_root)
        completed = subprocess.run(
            ["bash", str(hook), event],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def test_low_level_writer_mutation_requires_live_authority_transaction(self) -> None:
        target = self.repo / "must-not-exist" / "bypass.txt"
        with self.assertRaises(transfer.TransferError) as raised:
            writer.atomic_write_text(target, "unsafe")
        self.assertEqual(raised.exception.code, "write_not_fenced")
        self.assertFalse(target.parent.exists())

    def test_revoked_source_direct_writer_exits_before_any_output_mutation(self) -> None:
        self.acknowledge()
        before = self.tree_snapshot(self.repo)
        output = self.repo / "forbidden" / "capsule.md"
        completed = subprocess.run(
            [
                sys.executable,
                str(WRITER),
                "--repo",
                str(self.repo),
                "--session-id",
                self.SOURCE,
                "--source-session-id",
                self.SOURCE,
                "--out",
                str(output),
                "--update-active-task-only",
                "--objective",
                "forbidden",
                "--next-step",
                "forbidden",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 3)
        self.assertFalse(output.parent.exists())
        self.assertEqual(before, self.tree_snapshot(self.repo))

    def test_pending_destination_is_discovered_without_source_in_hook_payload(self) -> None:
        _transfer_id, exact = self.bind_destination()
        prompt = self.run_hook(
            PLUGIN_HOOK,
            "UserPromptSubmit",
            {"session_id": self.DESTINATION, "prompt": "verify and acknowledge"},
        )
        self.assertEqual(prompt, {"continue": True})
        denied = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "apply_patch",
                "tool_input": {},
            },
        )
        decision = denied["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], "destination_embargoed")

        allowed = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 {TRANSFER} --repo {self.repo} status --source-session-id {self.SOURCE}"
                },
            },
        )
        self.assertEqual(allowed, {"continue": True})
        injected = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 {TRANSFER} status --source-session-id {self.SOURCE}; touch pwned"
                },
            },
        )
        self.assertEqual(
            injected["hookSpecificOutput"]["permissionDecision"], "deny"
        )

        for command in (
            f"python3 {TRANSFER} --repo {self.repo} status --source-session-id {self.SOURCE} --unexpected value",
            f"python3 {TRANSFER} --repo {self.repo} status --source-session-id {self.SOURCE} --source-session-id duplicate",
            f"python3 {TRANSFER} --repo {self.repo} acknowledge --source-session-id {self.SOURCE}",
            f"python3 {TRANSFER} --repo {self.repo} status --source-session-id {self.SOURCE} $(touch pwned)",
            f"/tmp/python3 {TRANSFER} --repo {self.repo} status --source-session-id {self.SOURCE}",
            f"python3 {TRANSFER} --repo /tmp status --source-session-id {self.SOURCE}",
            f"python3 {TRANSFER} --repo {self.repo} status --source-session-id wrong-source",
        ):
            rejected = self.run_hook(
                PLUGIN_HOOK,
                "PreToolUse",
                {
                    "session_id": self.DESTINATION,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
            )
            self.assertEqual(
                rejected["hookSpecificOutput"]["permissionDecision"], "deny"
            )

        readonly = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.DESTINATION, "tool_name": "Read", "tool_input": {}},
        )
        self.assertEqual(readonly, {"continue": True})
        goal_read = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.DESTINATION, "tool_name": "get_goal", "tool_input": {}},
        )
        self.assertEqual(goal_read, {"continue": True})
        for tool_name in ("exec_command", "Bash", "shell"):
            for command in (
                "pwd",
                "git status --short",
                "git diff --stat",
                "git diff --name-only",
                "git rev-parse --show-toplevel",
            ):
                repo_read = self.run_hook(
                    PLUGIN_HOOK,
                    "PreToolUse",
                    {
                        "session_id": self.DESTINATION,
                        "tool_name": tool_name,
                        "tool_input": {"cmd": command, "workdir": str(self.repo)},
                    },
                )
                self.assertEqual(repo_read, {"continue": True})
        escaped_read = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "exec_command",
                "tool_input": {
                    "cmd": "git status --short --branch",
                    "workdir": str(self.repo),
                },
            },
        )
        self.assertEqual(
            escaped_read["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        wrong_workdir = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {"cmd": "pwd", "workdir": "/tmp"},
            },
        )
        self.assertEqual(
            wrong_workdir["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        unknown = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.DESTINATION, "tool_name": "mystery_tool", "tool_input": {}},
        )
        self.assertEqual(unknown["hookSpecificOutput"]["permissionDecision"], "deny")

        exact_acknowledge = (
            f"python3 {TRANSFER} --repo {self.repo} acknowledge"
            f" --source-session-id {exact['source_session_id']}"
            f" --transfer-id {exact['transfer_id']}"
            f" --destination-session-id {exact['destination_session_id']}"
            f" --destination-task-id {exact['destination_task_id']}"
            f" --goal-identity {exact['goal_identity']}"
            f" --capsule-path {exact['capsule_path']}"
            f" --capsule-revision {exact['capsule_revision']}"
            f" --capsule-sha256 {exact['capsule_sha256']}"
            f" --nonce {exact['nonce']}"
        )
        allowed_ack = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {"command": exact_acknowledge},
            },
        )
        self.assertEqual(allowed_ack, {"continue": True})
        wrong_nonce = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {"command": exact_acknowledge.replace(self.NONCE, "wrongnonceabcdefghijklmnopqrst")},
            },
        )
        self.assertEqual(wrong_nonce["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_unrelated_actor_remains_allowed_across_runtime_and_wrapper_fallbacks(self) -> None:
        self.acknowledge()
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        cases = (
            (PLUGIN_HOOK, REPO),
            (PLUGIN_HOOK, missing_plugin),
            (CODEX_HOOK, REPO),
            (WORKFLOW_HOOK, REPO),
        )
        for hook, plugin_root in cases:
            with self.subTest(hook=hook, plugin_root=plugin_root):
                response = self.run_hook(
                    hook,
                    "PreToolUse",
                    {"session_id": "unrelated-session", "tool_name": "apply_patch"},
                    plugin_root=plugin_root,
                )
                self.assertEqual(response, {"continue": True})

    def test_static_fallback_uses_top_level_actor_not_nested_tool_identity(self) -> None:
        self.acknowledge()
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        cases = (
            (PLUGIN_HOOK, missing_plugin),
            (CODEX_HOOK, REPO),
            (WORKFLOW_HOOK, REPO),
        )
        revoked_payload = {
            "session_id": self.SOURCE,
            "tool_name": "apply_patch",
            "tool_input": {"session_id": "unrelated-nested"},
        }
        unrelated_payload = {
            "session_id": "unrelated-top-level",
            "tool_name": "apply_patch",
            "tool_input": {"session_id": self.SOURCE},
        }
        for hook, plugin_root in cases:
            with self.subTest(hook=hook, actor="revoked"):
                denied = self.run_hook(
                    hook,
                    "PreToolUse",
                    revoked_payload,
                    plugin_root=plugin_root,
                )
                self.assertEqual(
                    denied["hookSpecificOutput"]["permissionDecision"], "deny"
                )
            with self.subTest(hook=hook, actor="unrelated"):
                allowed = self.run_hook(
                    hook,
                    "PreToolUse",
                    unrelated_payload,
                    plugin_root=plugin_root,
                )
                self.assertEqual(allowed, {"continue": True})

    def test_static_fallback_fails_closed_on_ambiguous_top_level_identity(self) -> None:
        self.acknowledge()
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        payload = (
            '{"session_id":"unrelated-one","thread_id":"unrelated-two",'
            '"tool_name":"apply_patch","tool_input":{}}'
        )
        env = os.environ.copy()
        env["ROOT"] = str(self.repo)
        env["PLUGIN_ROOT"] = str(missing_plugin)
        completed = subprocess.run(
            ["bash", str(PLUGIN_HOOK), "PreToolUse"],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        denied = json.loads(completed.stdout)
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_static_fallback_embargoes_pending_destination_but_allows_ack_prompt(self) -> None:
        self.bind_destination()
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        for hook in (PLUGIN_HOOK, CODEX_HOOK, WORKFLOW_HOOK):
            plugin_root = missing_plugin if hook == PLUGIN_HOOK else REPO
            denied = self.run_hook(
                hook,
                "PreToolUse",
                {"session_id": self.DESTINATION, "tool_name": "apply_patch"},
                plugin_root=plugin_root,
            )
            self.assertEqual(
                denied["hookSpecificOutput"]["permissionDecision"], "deny"
            )
            prompt = self.run_hook(
                hook,
                "UserPromptSubmit",
                {"session_id": self.DESTINATION, "prompt": "verify and acknowledge"},
                plugin_root=plugin_root,
            )
            self.assertEqual(prompt, {"continue": True})

    def test_session_lock_file_must_be_created_under_authority(self) -> None:
        context = load_module("context_handoff_lock_test", CONTEXT)
        missing = self.repo / "missing" / ".handoff.lock"
        with self.assertRaises(FileNotFoundError):
            with context.handoff_lock(missing):
                self.fail("missing lock file was created outside authority")
        self.assertFalse(missing.parent.exists())

    def test_authority_transaction_releases_thread_fence_on_exception(self) -> None:
        target = self.repo / "exception-bypass.txt"
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with transfer.authority_transaction(
                self.repo,
                actor_session_id=self.SOURCE,
                source_session_id=self.SOURCE,
            ):
                raise RuntimeError("boom")
        with self.assertRaises(transfer.TransferError) as raised:
            writer.atomic_write_text(target, "unsafe")
        self.assertEqual(raised.exception.code, "write_not_fenced")
        self.assertFalse(target.exists())

    def test_revoked_source_gets_event_specific_hook_envelopes(self) -> None:
        self.acknowledge()
        base = {"session_id": self.SOURCE, "source_session_id": self.SOURCE}
        prompt = self.run_hook(PLUGIN_HOOK, "UserPromptSubmit", base)
        self.assertEqual(prompt["decision"], "block")
        tool = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {**base, "tool_name": "Bash", "tool_input": {"command": "pwd"}},
        )
        self.assertEqual(tool["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertFalse(self.run_hook(PLUGIN_HOOK, "PreCompact", base)["continue"])
        self.assertFalse(self.run_hook(PLUGIN_HOOK, "Stop", base)["continue"])

    def test_static_fallback_denies_only_implicated_sessions(self) -> None:
        self.acknowledge()
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        source = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.SOURCE, "tool_name": "Bash", "tool_input": {"command": "pwd"}},
            plugin_root=missing_plugin,
        )
        self.assertEqual(source["hookSpecificOutput"]["permissionDecision"], "deny")
        destination = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.DESTINATION, "tool_name": "apply_patch"},
            plugin_root=missing_plugin,
        )
        self.assertEqual(destination["hookSpecificOutput"]["permissionDecision"], "deny")
        destination_prompt = self.run_hook(
            PLUGIN_HOOK,
            "UserPromptSubmit",
            {"session_id": self.DESTINATION, "prompt": "request source stop"},
            plugin_root=missing_plugin,
        )
        self.assertEqual(destination_prompt, {"continue": True})
        source_prompt = self.run_hook(
            PLUGIN_HOOK,
            "UserPromptSubmit",
            {"session_id": self.SOURCE, "prompt": "continue writing"},
            plugin_root=missing_plugin,
        )
        self.assertEqual(source_prompt["decision"], "block")
        unrelated = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": "unrelated-session", "tool_name": "apply_patch"},
            plugin_root=missing_plugin,
        )
        self.assertEqual(unrelated, {"continue": True})

        paths = transfer.transfer_paths(self.repo, self.SOURCE)
        paths.ownership.write_text("{corrupt", encoding="utf-8")
        unrelated_corrupt = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": "another-unrelated", "tool_name": "apply_patch"},
            plugin_root=missing_plugin,
        )
        self.assertEqual(unrelated_corrupt, {"continue": True})

    def test_static_fallback_both_blocks_after_tombstone_before_ownership(self) -> None:
        _transfer_id, exact = self.bind_destination()
        with mock.patch.dict(
            os.environ,
            {transfer.FAULT_ENV: "after_tombstone_before_ownership"},
        ):
            with self.assertRaises(transfer.FaultInjected):
                transfer.acknowledge(self.repo, **exact)
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        for actor in (self.SOURCE, self.DESTINATION):
            denied = self.run_hook(
                PLUGIN_HOOK,
                "PreToolUse",
                {"session_id": actor, "tool_name": "apply_patch"},
                plugin_root=missing_plugin,
            )
            self.assertEqual(
                denied["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_tombstone_receipt_discovers_destination_when_pointer_is_corrupt(self) -> None:
        _transfer_id, exact = self.bind_destination()
        with mock.patch.dict(
            os.environ,
            {transfer.FAULT_ENV: "after_tombstone_before_ownership"},
        ):
            with self.assertRaises(transfer.FaultInjected):
                transfer.acknowledge(self.repo, **exact)
        paths = transfer.transfer_paths(self.repo, self.SOURCE)
        paths.active.write_text("{corrupt", encoding="utf-8")
        runtime = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.DESTINATION, "tool_name": "apply_patch"},
        )
        self.assertEqual(runtime["hookSpecificOutput"]["permissionDecision"], "deny")
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        fallback = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.DESTINATION, "tool_name": "apply_patch"},
            plugin_root=missing_plugin,
        )
        self.assertEqual(fallback["hookSpecificOutput"]["permissionDecision"], "deny")
        prompt = self.run_hook(
            PLUGIN_HOOK,
            "UserPromptSubmit",
            {"session_id": self.DESTINATION, "prompt": "retry exact acknowledgement"},
            plugin_root=missing_plugin,
        )
        self.assertEqual(prompt, {"continue": True})

    def test_retained_record_keeps_pre_ack_destination_embargoed_without_pointer(self) -> None:
        self.bind_destination()
        paths = transfer.transfer_paths(self.repo, self.SOURCE)
        paths.active.write_text("{corrupt", encoding="utf-8")
        denied = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {"session_id": self.DESTINATION, "tool_name": "apply_patch"},
        )
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(
            denied["hookSpecificOutput"]["permissionDecisionReason"],
            "transfer_state_corrupt",
        )
        missing_plugin = self.repo / "missing-plugin"
        missing_plugin.mkdir()
        for hook, plugin_root in (
            (PLUGIN_HOOK, missing_plugin),
            (CODEX_HOOK, REPO),
            (WORKFLOW_HOOK, REPO),
        ):
            fallback = self.run_hook(
                hook,
                "PreToolUse",
                {"session_id": self.DESTINATION, "tool_name": "apply_patch"},
                plugin_root=plugin_root,
            )
            self.assertEqual(
                fallback["hookSpecificOutput"]["permissionDecision"], "deny"
            )

    def test_core_fallback_allows_exact_bound_control_when_context_is_missing(self) -> None:
        transfer_id, _exact = self.bind_destination()
        plugin_root = self.repo / "core-only-plugin"
        scripts = plugin_root / "skills" / "checkpoint-and-continue" / "scripts"
        scripts.mkdir(parents=True)
        installed_transfer = scripts / "transfer_control.py"
        shutil.copy2(TRANSFER, installed_transfer)
        pending_status = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 {installed_transfer} --repo {self.repo} status --source-session-id {self.SOURCE}"
                },
            },
            plugin_root=plugin_root,
        )
        self.assertEqual(pending_status, {"continue": True})
        transfer.acknowledge(self.repo, **_exact)
        request_stop = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 {installed_transfer} --repo {self.repo} request-stop --source-session-id {self.SOURCE} --transfer-id {transfer_id} --capability unsupported"
                },
            },
            plugin_root=plugin_root,
        )
        self.assertEqual(request_stop, {"continue": True})
        transfer.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="unsupported",
        )
        pending_record = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 {installed_transfer} --repo {self.repo} record-stop --source-session-id {self.SOURCE} --transfer-id {transfer_id} --result unsupported"
                },
            },
            plugin_root=plugin_root,
        )
        self.assertEqual(pending_record, {"continue": True})
        unproven_success = self.run_hook(
            PLUGIN_HOOK,
            "PreToolUse",
            {
                "session_id": self.DESTINATION,
                "tool_name": "Bash",
                "tool_input": {
                    "command": f"python3 {installed_transfer} --repo {self.repo} record-stop --source-session-id {self.SOURCE} --transfer-id {transfer_id} --result interrupted --evidence-kind native_interrupt_result --evidence-reference adapter://prompt/untrusted"
                },
            },
            plugin_root=plugin_root,
        )
        self.assertEqual(
            unproven_success["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_atomic_writer_cleans_temporary_file_when_replace_fails(self) -> None:
        target = self.repo / "state" / "target.json"
        with transfer.authority_transaction(
            self.repo,
            actor_session_id=self.SOURCE,
            source_session_id=self.SOURCE,
        ):
            with mock.patch.object(writer.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    writer.atomic_write_text(target, "payload")
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob("target.json.*.tmp")), [])

    def test_context_retry_reuses_prepare_intent_after_record_before_pointer_fault(self) -> None:
        source = "context-intent-source"
        command = [
            sys.executable,
            str(CONTEXT),
            "--repo", str(self.repo),
            "--trigger", "manual",
            "--session-id", source,
            "--source-session-id", source,
            "--objective", "Recover the exact prepared handoff",
            "--active-task", "Exercise context prepare recovery",
            "--phase", "implementation",
            "--status", "in progress",
            "--completion-criteria", "One exact transfer is prepared",
            "--completed-work", "Recovery test arranged",
            "--remaining-work", "Retry the interrupted prepare",
            "--constraints", "Reuse capsule path and nonce",
            "--decisions", "Persist prepare intent before writing",
            "--blockers", "Injected durability fault only",
            "--authoritative-files", str(CONTEXT),
            "--validation-status", "Focused retry test running",
            "--next-step", "Retry from the durable prepare intent",
            "--goal-objective", "Prove crash-safe context preparation",
            "--reason", "integration fault recovery",
            "--dedup-seconds", "0",
        ]
        fault_env = os.environ.copy()
        fault_env[transfer.FAULT_ENV] = "after_prepare_record_before_pointer"
        first = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=fault_env,
            check=False,
        )
        self.assertNotEqual(first.returncode, 0)
        context = load_module("context_handoff_intent_test", CONTEXT)
        state = context.state_paths(self.repo, source)
        intent = json.loads(state.prepare_intent.read_text(encoding="utf-8"))
        intended_path = intent["capsule_path"]
        intended_nonce = intent["transfer_nonce"]
        transfer_paths = transfer.transfer_paths(self.repo, source)
        self.assertEqual(len(list(transfer_paths.transfers.glob("*.json"))), 1)
        self.assertFalse(transfer_paths.active.exists())

        second = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        recovered = json.loads(second.stdout)
        self.assertEqual(recovered["capsule_path"], intended_path)
        self.assertEqual(recovered["transfer_nonce"], intended_nonce)
        self.assertEqual(len(list(transfer_paths.transfers.glob("*.json"))), 1)
        self.assertFalse(state.prepare_intent.exists())
        phases = [item["phase"] for item in recovered["lifecycle_next_actions"]]
        self.assertEqual(
            phases,
            [
                "launch_requested",
                "create_clean_task",
                "delivered_and_started",
                "verify_and_acknowledge",
                "source_stop",
            ],
        )
        verify_step = recovered["lifecycle_next_actions"][3]
        self.assertEqual(
            [command[command.index("--repo") + 2] for command in verify_step["commands_argv"]],
            ["verify", "acknowledge"],
        )
        guidance = context.app_capability_guidance(
            {"available_thread_tools": ["create_thread", "read_thread", "handoff_thread"]},
            repo=self.repo,
            transfer={
                "session_id": source,
                "transfer_id": recovered["transfer_id"],
            },
        )
        self.assertFalse(guidance["target_interrupt_isolation_supported"])
        self.assertTrue(guidance["handoff_thread_candidate_available"])
        self.assertIn("never generic interrupt or close", guidance["handoff_thread_rule"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
