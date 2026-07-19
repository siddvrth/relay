#!/usr/bin/env python3
"""Focused hostile tests for the acknowledgement/ownership core."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("transfer_control.py")
SPEC = importlib.util.spec_from_file_location("transfer_control", SCRIPT)
assert SPEC and SPEC.loader
transfer_control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = transfer_control
SPEC.loader.exec_module(transfer_control)


class TransferControlTests(unittest.TestCase):
    SOURCE = "source-session"
    DESTINATION = "destination-session"
    TASK = "destination-task"
    GOAL = "goal:sha256:example"
    NONCE = "0123456789abcdefghijklmnopqrstuv"
    NEXT_ACTION = "Run the focused transfer tests"
    VALIDATION_COMMAND = "python3 focused_test.py"
    VALIDATION_EXPECTED = "exit 0 and 7 tests pass"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        session_dir = transfer_control.transfer_paths(self.repo, self.SOURCE).session_dir
        session_dir.mkdir(parents=True)
        self.capsule = session_dir / "capsule.md"
        self.capsule.write_text("exact ready capsule\n", encoding="utf-8")
        self.sha = hashlib.sha256(self.capsule.read_bytes()).hexdigest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, revision: int = 1, nonce: str | None = None) -> dict[str, object]:
        return transfer_control.prepare(
            self.repo,
            source_session_id=self.SOURCE,
            goal_identity=self.GOAL,
            capsule_path=str(self.capsule),
            capsule_revision=revision,
            capsule_sha256=self.sha,
            resume_ready=True,
            next_action=self.NEXT_ACTION,
            validation_evidence=[],
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
            nonce=nonce or self.NONCE,
        )

    def launch_and_start(self, transfer_id: str) -> None:
        transfer_control.launch_requested(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
        )
        transfer_control.delivered(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
            destination_task_id=self.TASK,
        )
        transfer_control.started(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            destination_session_id=self.DESTINATION,
            destination_task_id=self.TASK,
        )

    def exact(self, transfer_id: str) -> dict[str, object]:
        return {
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

    def verify(self, transfer_id: str) -> None:
        transfer_control.verify(
            self.repo,
            **self.exact(transfer_id),
            repository_inspected=True,
            goal_inspected=True,
            exact_next_action=self.NEXT_ACTION,
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
        )

    def ready_for_ack(self) -> tuple[str, dict[str, object]]:
        prepared = self.prepare()
        transfer_id = str(prepared["transfer_id"])
        self.launch_and_start(transfer_id)
        self.verify(transfer_id)
        return transfer_id, self.exact(transfer_id)

    def test_prepare_is_idempotent_for_same_revision_and_capsule(self) -> None:
        first = self.prepare()
        second = self.prepare(nonce="abcdefghijklmnopqrstuvwxyz012345")
        self.assertEqual(first["transfer_id"], second["transfer_id"])
        self.assertTrue(second["idempotent"])
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        self.assertEqual(len(list(paths.transfers.glob("*.json"))), 1)

    def test_resume_validation_mismatch_fails_then_exact_values_persist_idempotently(self) -> None:
        prepared = transfer_control.prepare(
            self.repo,
            source_session_id=self.SOURCE,
            goal_identity=self.GOAL,
            capsule_path=str(self.capsule),
            capsule_revision=1,
            capsule_sha256=self.sha,
            resume_ready=True,
            next_action=self.NEXT_ACTION,
            validation_evidence=[],
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
            nonce=self.NONCE,
        )
        transfer_id = str(prepared["transfer_id"])
        self.launch_and_start(transfer_id)

        mismatches = (
            ("wrong action", self.VALIDATION_COMMAND, self.VALIDATION_EXPECTED),
            (self.NEXT_ACTION, "python3 other_test.py", self.VALIDATION_EXPECTED),
            (self.NEXT_ACTION, self.VALIDATION_COMMAND, "exit 0 and 8 tests pass"),
        )
        for action, command, expected in mismatches:
            with self.subTest(action=action, command=command, expected=expected):
                with self.assertRaises(transfer_control.TransferError) as raised:
                    transfer_control.verify(
                        self.repo,
                        **self.exact(transfer_id),
                        repository_inspected=True,
                        goal_inspected=True,
                        exact_next_action=action,
                        resume_validation_command=command,
                        resume_validation_expected=expected,
                    )
                self.assertEqual(
                    raised.exception.code, "capsule_verification_failed"
                )

        verified = transfer_control.verify(
            self.repo,
            **self.exact(transfer_id),
            repository_inspected=True,
            goal_inspected=True,
            exact_next_action=self.NEXT_ACTION,
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
        )
        repeated = transfer_control.verify(
            self.repo,
            **self.exact(transfer_id),
            repository_inspected=True,
            goal_inspected=True,
            exact_next_action=self.NEXT_ACTION,
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
        )
        self.assertEqual(
            verified["verification"]["resume_validation"],
            {"command": self.VALIDATION_COMMAND, "expected": self.VALIDATION_EXPECTED},
        )
        self.assertTrue(repeated["idempotent"])
        transfer_control.acknowledge(self.repo, **self.exact(transfer_id))
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        tombstone = json.loads(paths.tombstone.read_text(encoding="utf-8"))
        receipt = tombstone["receipt"]
        self.assertEqual(receipt["next_action"], self.NEXT_ACTION)
        self.assertEqual(receipt["validation_evidence"], [])
        self.assertEqual(
            receipt["resume_validation"],
            {"command": self.VALIDATION_COMMAND, "expected": self.VALIDATION_EXPECTED},
        )

    def test_history_cannot_substitute_for_bound_resume_validation(self) -> None:
        prepared = transfer_control.prepare(
            self.repo,
            source_session_id=self.SOURCE,
            goal_identity=self.GOAL,
            capsule_path=str(self.capsule),
            capsule_revision=1,
            capsule_sha256=self.sha,
            resume_ready=True,
            next_action=self.NEXT_ACTION,
            validation_evidence=["Historical suite passed yesterday"],
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
            nonce=self.NONCE,
        )
        transfer_id = str(prepared["transfer_id"])
        self.launch_and_start(transfer_id)

        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.verify(
                self.repo,
                **self.exact(transfer_id),
                repository_inspected=True,
                goal_inspected=True,
                exact_next_action=self.NEXT_ACTION,
                resume_validation_command="Historical suite passed yesterday",
                resume_validation_expected=self.VALIDATION_EXPECTED,
            )
        self.assertEqual(raised.exception.code, "capsule_verification_failed")

        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        record_path = paths.transfers / f"{transfer_id}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record.pop("resume_validation")
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(transfer_control.TransferError) as unbound:
            transfer_control.verify(
                self.repo,
                **self.exact(transfer_id),
                repository_inspected=True,
                goal_inspected=True,
                exact_next_action=self.NEXT_ACTION,
                resume_validation_command=self.VALIDATION_COMMAND,
                resume_validation_expected=self.VALIDATION_EXPECTED,
            )
        self.assertEqual(unbound.exception.code, "capsule_verification_failed")
        record["phase"] = "resume_verified"
        record["verification"] = {
            "result": "verified",
            "exact_next_action": self.NEXT_ACTION,
            "smallest_validation": "Historical suite passed yesterday",
        }
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(transfer_control.TransferError) as acknowledge_error:
            transfer_control.acknowledge(
                self.repo,
                **self.exact(transfer_id),
            )
        self.assertEqual(
            acknowledge_error.exception.code, "capsule_verification_failed"
        )

    def test_unknown_launch_blocks_blind_second_create_and_reconciles_exact_nonce(self) -> None:
        transfer_id = str(self.prepare()["transfer_id"])
        transfer_control.launch_requested(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
        )
        transfer_control.record_launch_outcome(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            outcome="unknown",
            detail="remote result lost",
        )
        with self.assertRaisesRegex(transfer_control.TransferError, "reconcile"):
            transfer_control.launch_requested(
                self.repo,
                source_session_id=self.SOURCE,
                transfer_id=transfer_id,
                transport_key="transport-2",
            )
        reconciled = transfer_control.reconcile_launch(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
            observed_nonce=self.NONCE,
            destination_session_id=self.DESTINATION,
            destination_task_id=self.TASK,
        )
        self.assertEqual(reconciled["phase"], "clean_session_started")

    def test_unknown_launch_cannot_be_marked_delivered_without_reconciliation(self) -> None:
        transfer_id = str(self.prepare()["transfer_id"])
        transfer_control.launch_requested(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
        )
        transfer_control.record_launch_outcome(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            outcome="unknown",
        )
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.delivered(
                self.repo,
                source_session_id=self.SOURCE,
                transfer_id=transfer_id,
                transport_key="transport-1",
                destination_task_id=self.TASK,
            )
        self.assertEqual(raised.exception.code, "launch_outcome_unknown")

    def test_launch_reconciliation_validates_before_one_atomic_state_transition(self) -> None:
        transfer_id = str(self.prepare()["transfer_id"])
        transfer_control.launch_requested(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
        )
        transfer_control.record_launch_outcome(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            outcome="unknown",
        )
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        before_record = transfer_control._record_path(paths, transfer_id).read_bytes()
        before_pointer = paths.active.read_bytes()
        with self.assertRaises(transfer_control.TransferError):
            transfer_control.reconcile_launch(
                self.repo,
                source_session_id=self.SOURCE,
                transfer_id=transfer_id,
                transport_key="transport-1",
                observed_nonce=self.NONCE,
                destination_session_id=" ",
                destination_task_id=self.TASK,
            )
        self.assertEqual(before_record, transfer_control._record_path(paths, transfer_id).read_bytes())
        self.assertEqual(before_pointer, paths.active.read_bytes())

        result = transfer_control.reconcile_launch(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            transport_key="transport-1",
            observed_nonce=self.NONCE,
            destination_session_id=self.DESTINATION,
            destination_task_id=self.TASK,
        )
        self.assertEqual(result["phase"], "clean_session_started")
        record = json.loads(transfer_control._record_path(paths, transfer_id).read_text())
        self.assertEqual(
            record["launch"]["reconciled_destination_session_id"], self.DESTINATION
        )
        self.assertEqual(record["launch"]["reconciled_destination_task_id"], self.TASK)
        with self.assertRaises(transfer_control.TransferError):
            transfer_control.started(
                self.repo,
                source_session_id=self.SOURCE,
                transfer_id=transfer_id,
                destination_session_id="other-destination",
                destination_task_id=self.TASK,
            )

    def test_verification_mismatch_preserves_source_authority(self) -> None:
        transfer_id = str(self.prepare()["transfer_id"])
        self.launch_and_start(transfer_id)
        mismatched = self.exact(transfer_id)
        mismatched["goal_identity"] = "wrong-goal"
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.verify(
                self.repo,
                **mismatched,
                repository_inspected=True,
                goal_inspected=True,
                exact_next_action="next",
                resume_validation_command=self.VALIDATION_COMMAND,
                resume_validation_expected=self.VALIDATION_EXPECTED,
            )
        self.assertEqual(raised.exception.code, "replayed_acknowledgement")
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        self.assertFalse(paths.ownership.exists())
        self.assertFalse(paths.tombstone.exists())
        self.assertTrue(
            transfer_control.guard_write(
                self.repo,
                actor_session_id=self.SOURCE,
                source_session_id=self.SOURCE,
            )["allowed"]
        )

    def test_ack_commits_tombstone_then_destination_ownership_but_embargoes_work(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        result = transfer_control.acknowledge(self.repo, **exact)
        self.assertFalse(result["can_continue"])
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        tombstone = json.loads(paths.tombstone.read_text(encoding="utf-8"))
        ownership = json.loads(paths.ownership.read_text(encoding="utf-8"))
        self.assertTrue(tombstone["enforced_read_only"])
        self.assertEqual(ownership["sole_writer_session_id"], self.DESTINATION)
        self.assertEqual(ownership["ownership_epoch"], 1)
        self.assertFalse(
            transfer_control.guard_write(
                self.repo,
                actor_session_id=self.SOURCE,
                source_session_id=self.SOURCE,
            )["allowed"]
        )
        destination_guard = transfer_control.guard_write(
            self.repo,
            actor_session_id=self.DESTINATION,
            source_session_id=self.SOURCE,
        )
        self.assertFalse(destination_guard["allowed"])
        self.assertEqual(destination_guard["reason"], "destination_embargoed")

    def test_authority_transaction_holds_lock_across_write_and_fences_late_source(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        ack_started = threading.Event()
        ack_finished = threading.Event()
        failures: list[BaseException] = []

        def acknowledge_in_thread() -> None:
            ack_started.set()
            try:
                transfer_control.acknowledge(self.repo, **exact)
            except BaseException as error:  # pragma: no cover - asserted below.
                failures.append(error)
            finally:
                ack_finished.set()

        worker = threading.Thread(target=acknowledge_in_thread)
        with transfer_control.authority_transaction(
            self.repo,
            actor_session_id=self.SOURCE,
            source_session_id=self.SOURCE,
        ) as fence:
            self.assertEqual(fence.ownership_epoch, 0)
            worker.start()
            self.assertTrue(ack_started.wait(1))
            time.sleep(0.05)
            self.assertFalse(ack_finished.is_set())
            (self.repo / "fenced-write.txt").write_text("committed under lock\n")
        worker.join(2)
        self.assertTrue(ack_finished.is_set())
        self.assertEqual(failures, [])
        with self.assertRaises(transfer_control.TransferError) as raised:
            with transfer_control.authority_transaction(
                self.repo,
                actor_session_id=self.SOURCE,
                source_session_id=self.SOURCE,
            ):
                self.fail("revoked source entered a fenced write")
        self.assertEqual(raised.exception.code, "write_not_authorized")

    def test_write_authority_requires_explicit_nonblank_source_scope(self) -> None:
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.guard_write(
                self.repo,
                actor_session_id=self.SOURCE,
                source_session_id="",
            )
        self.assertEqual(raised.exception.code, "invalid_identity")
        cli = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                str(self.repo),
                "guard-write",
                "--actor-session-id",
                self.SOURCE,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(cli.returncode, 0)
        self.assertIn("--source-session-id", cli.stderr)
        with self.assertRaises(transfer_control.TransferError) as raised:
            with transfer_control.authority_transaction(
                self.repo,
                actor_session_id=self.SOURCE,
                source_session_id=" ",
            ):
                self.fail("empty source scope entered authority transaction")
        self.assertEqual(raised.exception.code, "invalid_identity")

    def test_exact_duplicate_ack_is_idempotent(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        first = transfer_control.acknowledge(self.repo, **exact)
        second = transfer_control.acknowledge(self.repo, **exact)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        ownership = transfer_control.status(
            self.repo, source_session_id=self.SOURCE
        )["ownership"]
        self.assertEqual(ownership["ownership_epoch"], 1)

    def test_started_and_verified_retries_remain_idempotent_after_later_phases(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        repeated_start = transfer_control.started(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            destination_session_id=self.DESTINATION,
            destination_task_id=self.TASK,
        )
        self.assertTrue(repeated_start["idempotent"])
        transfer_control.acknowledge(self.repo, **exact)
        repeated_verify = transfer_control.verify(
            self.repo,
            **exact,
            repository_inspected=True,
            goal_inspected=True,
            exact_next_action="Run the focused transfer tests",
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
        )
        self.assertTrue(repeated_verify["idempotent"])
        self.assertEqual(repeated_verify["phase"], "acknowledged")

    def test_unrelated_source_chain_is_not_blocked_by_another_valid_ownership(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        unrelated = transfer_control.guard_write(
            self.repo,
            actor_session_id="unrelated-source",
            source_session_id="unrelated-source",
        )
        self.assertTrue(unrelated["allowed"])
        self.assertEqual(unrelated["reason"], "source_authority")
        affected = transfer_control.guard_write(
            self.repo,
            actor_session_id=self.SOURCE,
            source_session_id=self.SOURCE,
        )
        self.assertFalse(affected["allowed"])
        self.assertEqual(affected["reason"], "actor_revoked")

    def test_retained_record_discovers_pending_destination_without_active_pointer(self) -> None:
        transfer_id, _exact = self.ready_for_ack()
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        paths.active.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(
            transfer_control.discover_source_for_actor(self.repo, self.DESTINATION),
            self.SOURCE,
        )
        denied = transfer_control.guard_write(
            self.repo,
            actor_session_id=self.DESTINATION,
            source_session_id=self.SOURCE,
        )
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "transfer_state_corrupt")
        self.assertTrue(transfer_control._record_path(paths, transfer_id).exists())

    def test_owned_lifecycle_repairs_corrupt_active_pointer_from_ownership(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        paths.active.write_text("{corrupt", encoding="utf-8")
        current = transfer_control.status(self.repo, source_session_id=self.SOURCE)
        self.assertEqual(current["phase"], "acknowledged")
        self.assertEqual(json.loads(paths.active.read_text())["transfer_id"], transfer_id)

        paths.active.write_text("{corrupt", encoding="utf-8")
        requested = transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="unsupported",
        )
        self.assertEqual(requested["phase"], "source_stop_requested")

        paths.active.write_text("{corrupt", encoding="utf-8")
        recorded = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="unsupported",
        )
        self.assertTrue(recorded["can_continue"])
        self.assertEqual(json.loads(paths.active.read_text())["phase"], "source_stop_requested")

    def test_cross_session_ack_is_rejected_before_revocation(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        exact["destination_session_id"] = "attacker-session"
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.acknowledge(self.repo, **exact)
        self.assertEqual(raised.exception.code, "cross_session_acknowledgement")
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        self.assertFalse(paths.tombstone.exists())
        self.assertFalse(paths.ownership.exists())

    def test_timeout_allows_late_exact_ack_until_newer_transfer_exists(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        timed_out = transfer_control.acknowledgement_timeout(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
        )
        self.assertEqual(timed_out["failure"]["code"], "acknowledgement_timed_out")
        accepted = transfer_control.acknowledge(self.repo, **exact)
        self.assertEqual(accepted["phase"], "acknowledged")

    def test_newer_active_transfer_makes_old_ack_stale(self) -> None:
        old_id, old_exact = self.ready_for_ack()
        self.capsule.write_text("newer exact ready capsule\n", encoding="utf-8")
        self.sha = hashlib.sha256(self.capsule.read_bytes()).hexdigest()
        self.prepare(revision=2, nonce="zyxwvutsrqponmlkjihgfedcba987654")
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.acknowledge(self.repo, **old_exact)
        self.assertEqual(raised.exception.code, "stale_acknowledgement")
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        self.assertFalse(paths.tombstone.exists())
        self.assertFalse(paths.ownership.exists())
        self.assertNotEqual(old_id, json.loads(paths.active.read_text())["transfer_id"])

    def test_fault_after_tombstone_is_both_blocked_until_exact_retry(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        with mock.patch.dict(os.environ, {transfer_control.FAULT_ENV: "after_tombstone_before_ownership"}):
            with self.assertRaises(transfer_control.FaultInjected):
                transfer_control.acknowledge(self.repo, **exact)
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        self.assertTrue(paths.tombstone.exists())
        self.assertFalse(paths.ownership.exists())
        self.assertFalse(
            transfer_control.guard_write(
                self.repo,
                actor_session_id=self.SOURCE,
                source_session_id=self.SOURCE,
            )["allowed"]
        )
        self.assertFalse(
            transfer_control.guard_write(
                self.repo,
                actor_session_id=self.DESTINATION,
                source_session_id=self.SOURCE,
            )["allowed"]
        )
        recovered = transfer_control.acknowledge(self.repo, **exact)
        self.assertEqual(recovered["phase"], "acknowledged")

    def test_fault_after_ownership_reconciles_journal_without_second_epoch(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        with mock.patch.dict(os.environ, {transfer_control.FAULT_ENV: "after_ownership_before_journal"}):
            with self.assertRaises(transfer_control.FaultInjected):
                transfer_control.acknowledge(self.repo, **exact)
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        ownership = json.loads(paths.ownership.read_text())
        self.assertEqual(ownership["ownership_epoch"], 1)
        recovered = transfer_control.acknowledge(self.repo, **exact)
        self.assertTrue(recovered["idempotent"])
        ownership = json.loads(paths.ownership.read_text())
        self.assertEqual(ownership["ownership_epoch"], 1)
        self.assertEqual(recovered["phase"], "acknowledged")

    def test_stop_result_fault_reconciles_from_authoritative_ownership(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="unsupported",
        )
        with mock.patch.dict(
            os.environ,
            {transfer_control.FAULT_ENV: "after_stop_ownership_before_journal"},
        ):
            with self.assertRaises(transfer_control.FaultInjected):
                transfer_control.record_stop(
                    self.repo,
                    source_session_id=self.SOURCE,
                    transfer_id=transfer_id,
                    result="unsupported",
                    detail="no native target interrupt",
                )
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        ownership = json.loads(paths.ownership.read_text(encoding="utf-8"))
        self.assertTrue(ownership["source_stop"]["termination_pending"])
        recovered = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="unsupported",
            detail="no native target interrupt",
        )
        self.assertTrue(recovered["idempotent"])
        self.assertTrue(recovered["can_continue"])
        current = transfer_control.status(self.repo, source_session_id=self.SOURCE)
        self.assertTrue(current["termination_pending"])
        self.assertTrue(current["can_continue"])

    def test_stop_journal_fault_repairs_pointer_without_repeating_transition(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="native_interrupt",
        )
        with mock.patch.dict(
            os.environ,
            {transfer_control.FAULT_ENV: "after_stop_journal_before_pointer"},
        ):
            with self.assertRaises(transfer_control.FaultInjected):
                transfer_control.record_stop(
                    self.repo,
                    source_session_id=self.SOURCE,
                    transfer_id=transfer_id,
                    result="interrupted",
                    evidence_kind="native_interrupt_result",
                    evidence_reference="adapter://interrupt/observed-1",
                )
        recovered = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="interrupted",
            evidence_kind="native_interrupt_result",
            evidence_reference="adapter://interrupt/observed-1",
        )
        self.assertTrue(recovered["idempotent"])
        current = transfer_control.status(self.repo, source_session_id=self.SOURCE)
        self.assertEqual(current["phase"], "source_quiesced")
        self.assertTrue(current["can_continue"])

    def test_unsupported_stop_is_truthful_pending_and_releases_destination_embargo(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="unsupported",
        )
        result = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="unsupported",
            detail="host exposes no target interrupt",
        )
        self.assertTrue(result["can_continue"])
        repeated = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="unsupported",
            detail="host exposes no target interrupt",
        )
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["can_continue"], result["can_continue"])
        current = transfer_control.status(self.repo, source_session_id=self.SOURCE)
        self.assertTrue(current["termination_pending"])
        self.assertTrue(current["can_continue"])
        self.assertEqual(current["phase"], "source_stop_requested")
        self.assertTrue(
            transfer_control.guard_write(
                self.repo,
                actor_session_id=self.DESTINATION,
                source_session_id=self.SOURCE,
            )["allowed"]
        )

    def test_observed_quiescence_releases_destination_embargo(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="native_interrupt",
        )
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.record_stop(
                self.repo,
                source_session_id=self.SOURCE,
                transfer_id=transfer_id,
                result="interrupted",
            )
        self.assertEqual(raised.exception.code, "invalid_stop_evidence")
        result = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="interrupted",
            evidence_kind="native_interrupt_result",
            evidence_reference="adapter://interrupt/observed-2",
        )
        self.assertTrue(result["can_continue"])
        self.assertEqual(result["phase"], "source_quiesced")
        self.assertFalse(
            transfer_control.status(self.repo, source_session_id=self.SOURCE)[
                "termination_pending"
            ]
        )
        repeated = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="interrupted",
            evidence_kind="native_interrupt_result",
            evidence_reference="adapter://interrupt/observed-2",
        )
        self.assertTrue(repeated["idempotent"])
        ownership = transfer_control.status(
            self.repo, source_session_id=self.SOURCE
        )["ownership"]
        self.assertEqual(
            ownership["source_stop"]["adapter_evidence"]["reference"],
            "adapter://interrupt/observed-2",
        )
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.record_stop(
                self.repo,
                source_session_id=self.SOURCE,
                transfer_id=transfer_id,
                result="interrupted",
                evidence_kind="native_interrupt_result",
                evidence_reference="adapter://interrupt/different",
            )
        self.assertEqual(raised.exception.code, "stop_result_conflict")

    def test_impossible_stop_capability_result_combinations_fail_before_idempotence(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="unsupported",
        )
        for result, kind in (
            ("interrupted", "native_interrupt_result"),
            ("quiesced", "cooperative_quiescence"),
            ("already_exited", "native_exit_observation"),
        ):
            with self.subTest(result=result):
                with self.assertRaises(transfer_control.TransferError) as raised:
                    transfer_control.record_stop(
                        self.repo,
                        source_session_id=self.SOURCE,
                        transfer_id=transfer_id,
                        result=result,
                        evidence_kind=kind,
                        evidence_reference="adapter://unavailable/impossible",
                    )
                self.assertEqual(raised.exception.code, "invalid_stop_evidence")
        pending = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="unsupported",
        )
        self.assertTrue(pending["can_continue"])
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.record_stop(
                self.repo,
                source_session_id=self.SOURCE,
                transfer_id=transfer_id,
                result="interrupted",
                evidence_kind="native_interrupt_result",
                evidence_reference="adapter://unavailable/impossible",
            )
        self.assertEqual(raised.exception.code, "invalid_stop_evidence")

    def test_cli_host_adapter_can_record_evidence_bound_success(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="cooperative",
        )
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo", str(self.repo),
            "record-stop",
            "--source-session-id", self.SOURCE,
            "--transfer-id", transfer_id,
            "--result", "quiesced",
            "--evidence-kind", "cooperative_quiescence",
            "--evidence-reference", "adapter://cooperative/receipt-1",
        ]
        first = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
        self.assertTrue(json.loads(first.stdout)["can_continue"])
        second = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
        self.assertTrue(json.loads(second.stdout)["idempotent"])

    def test_chained_handoff_checks_original_actor_tombstone_and_advances_epoch(self) -> None:
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="unsupported",
        )
        transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="unsupported",
        )

        source = self.DESTINATION
        destination = "destination-session-2"
        task = "destination-task-2"
        paths = transfer_control.transfer_paths(self.repo, source)
        paths.session_dir.mkdir(parents=True, exist_ok=True)
        capsule = paths.session_dir / "r2-handoff.md"
        capsule.write_text("second exact ready capsule\n", encoding="utf-8")
        digest = hashlib.sha256(capsule.read_bytes()).hexdigest()
        nonce = "abcdefghijklmnopqrstuv0123456789"
        prepared = transfer_control.prepare(
            self.repo,
            source_session_id=source,
            goal_identity=self.GOAL,
            capsule_path=str(capsule),
            capsule_revision=2,
            capsule_sha256=digest,
            resume_ready=True,
            next_action="continue second transfer",
            validation_evidence=[],
            resume_validation_command="focused test",
            resume_validation_expected="focused test passes",
            nonce=nonce,
        )
        next_id = str(prepared["transfer_id"])
        transfer_control.launch_requested(
            self.repo,
            source_session_id=source,
            transfer_id=next_id,
            transport_key="transport-2",
        )
        transfer_control.delivered(
            self.repo,
            source_session_id=source,
            transfer_id=next_id,
            transport_key="transport-2",
            destination_task_id=task,
        )
        transfer_control.started(
            self.repo,
            source_session_id=source,
            transfer_id=next_id,
            destination_session_id=destination,
            destination_task_id=task,
        )
        second_exact = {
            "source_session_id": source,
            "transfer_id": next_id,
            "destination_session_id": destination,
            "destination_task_id": task,
            "goal_identity": self.GOAL,
            "capsule_path": str(capsule),
            "capsule_revision": 2,
            "capsule_sha256": digest,
            "nonce": nonce,
        }
        transfer_control.verify(
            self.repo,
            **second_exact,
            repository_inspected=True,
            goal_inspected=True,
            exact_next_action="continue second transfer",
            resume_validation_command="focused test",
            resume_validation_expected="focused test passes",
        )
        accepted = transfer_control.acknowledge(self.repo, **second_exact)
        self.assertEqual(accepted["ownership_epoch"], 2)
        denied = transfer_control.guard_write(
            self.repo,
            actor_session_id=self.SOURCE,
            source_session_id=source,
        )
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "actor_revoked")

    def test_ownership_tampering_fails_closed_for_every_actor(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        ownership = json.loads(paths.ownership.read_text(encoding="utf-8"))
        ownership["sole_writer_session_id"] = "attacker"
        paths.ownership.write_text(json.dumps(ownership), encoding="utf-8")
        for actor in (self.SOURCE, self.DESTINATION, "attacker"):
            denied = transfer_control.guard_write(
                self.repo,
                actor_session_id=actor,
                source_session_id=self.SOURCE,
            )
            self.assertFalse(denied["allowed"])
            expected = "actor_revoked" if actor == self.SOURCE else "ownership_corrupt"
            self.assertEqual(denied["reason"], expected)

    def test_tombstone_receipt_digest_tampering_fails_closed(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        tombstone = json.loads(paths.tombstone.read_text(encoding="utf-8"))
        tombstone["receipt"]["goal_identity"] = "tampered-goal"
        paths.tombstone.write_text(json.dumps(tombstone), encoding="utf-8")
        denied = transfer_control.guard_write(
            self.repo,
            actor_session_id=self.DESTINATION,
            source_session_id=self.SOURCE,
        )
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "ownership_corrupt")

    def test_self_consistent_receipt_forgery_cannot_escape_current_verification_binding(self) -> None:
        _transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        tombstone = json.loads(paths.tombstone.read_text(encoding="utf-8"))
        receipt = tombstone["receipt"]
        receipt["verification_digest"] = "0" * 64
        unsigned = dict(receipt)
        unsigned.pop("receipt_digest")
        receipt["receipt_digest"] = transfer_control._digest(unsigned)
        tombstone = transfer_control.durable_write_json(paths.tombstone, tombstone)
        ownership = json.loads(paths.ownership.read_text(encoding="utf-8"))
        ownership["receipt_digest"] = receipt["receipt_digest"]
        ownership["tombstone_digest"] = transfer_control._digest(tombstone)
        transfer_control.durable_write_json(paths.ownership, ownership)
        denied = transfer_control.guard_write(
            self.repo,
            actor_session_id=self.DESTINATION,
            source_session_id=self.SOURCE,
        )
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "ownership_corrupt")

    def test_prepare_rejects_external_and_symlink_escape_capsules(self) -> None:
        external = self.repo / "outside.md"
        external.write_text("outside\n", encoding="utf-8")
        digest = hashlib.sha256(external.read_bytes()).hexdigest()
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.prepare(
                self.repo,
                source_session_id=self.SOURCE,
                goal_identity=self.GOAL,
                capsule_path=str(external),
                capsule_revision=1,
                capsule_sha256=digest,
                resume_ready=True,
                next_action=self.NEXT_ACTION,
                validation_evidence=[],
                resume_validation_command=self.VALIDATION_COMMAND,
                resume_validation_expected=self.VALIDATION_EXPECTED,
                nonce=self.NONCE,
            )
        self.assertEqual(raised.exception.code, "unsafe_capsule_path")
        symlink = transfer_control.transfer_paths(self.repo, self.SOURCE).session_dir / "escape.md"
        symlink.symlink_to(external)
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.prepare(
                self.repo,
                source_session_id=self.SOURCE,
                goal_identity=self.GOAL,
                capsule_path=str(symlink),
                capsule_revision=1,
                capsule_sha256=digest,
                resume_ready=True,
                next_action=self.NEXT_ACTION,
                validation_evidence=[],
                resume_validation_command=self.VALIDATION_COMMAND,
                resume_validation_expected=self.VALIDATION_EXPECTED,
                nonce=self.NONCE,
            )
        self.assertEqual(raised.exception.code, "unsafe_capsule_path")

    def test_pointer_identity_mismatch_and_predecessor_rollback_fail_closed(self) -> None:
        self.prepare()
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        original_pointer = json.loads(paths.active.read_text(encoding="utf-8"))
        mismatched = dict(original_pointer)
        mismatched["goal_identity"] = "tampered"
        transfer_control.durable_write_json(paths.active, mismatched)
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.status(self.repo, source_session_id=self.SOURCE)
        self.assertEqual(raised.exception.code, "corrupt_state")

        transfer_control.durable_write_json(paths.active, original_pointer)
        self.capsule.write_text("revision two\n", encoding="utf-8")
        self.sha = hashlib.sha256(self.capsule.read_bytes()).hexdigest()
        self.prepare(revision=2, nonce="zyxwvutsrqponmlkjihgfedcba987654")
        transfer_control.durable_write_json(paths.active, original_pointer)
        with self.assertRaises(transfer_control.TransferError) as raised:
            transfer_control.status(self.repo, source_session_id=self.SOURCE)
        self.assertEqual(raised.exception.code, "pointer_rollback")

    def test_locking_unavailable_fails_closed(self) -> None:
        with mock.patch.object(transfer_control, "fcntl", None):
            with self.assertRaises(transfer_control.TransferError) as raised:
                self.prepare()
        self.assertEqual(raised.exception.code, "locking_unavailable")

    def test_unproven_process_group_capability_is_explicitly_unavailable(self) -> None:
        self.assertIn(
            "process_group_interruption_unavailable", transfer_control.STOP_CAPABILITIES
        )
        self.assertFalse(hasattr(transfer_control, "interrupt_registered_process_group"))
        transfer_id, exact = self.ready_for_ack()
        transfer_control.acknowledge(self.repo, **exact)
        requested = transfer_control.request_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            capability="process_group_interruption_unavailable",
        )
        self.assertEqual(requested["process_group_interruption"]["available"], False)
        pending = transfer_control.record_stop(
            self.repo,
            source_session_id=self.SOURCE,
            transfer_id=transfer_id,
            result="unsupported",
            detail="PID/PGID start-token provenance belongs in a host adapter",
        )
        self.assertTrue(pending["can_continue"])
        current = transfer_control.status(self.repo, source_session_id=self.SOURCE)
        self.assertFalse(current["process_group_interruption"]["available"])

    def test_prepare_recovers_exact_orphan_after_record_before_pointer_fault(self) -> None:
        with mock.patch.dict(
            os.environ,
            {transfer_control.FAULT_ENV: "after_prepare_record_before_pointer"},
        ):
            with self.assertRaises(transfer_control.FaultInjected):
                self.prepare()
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        self.assertFalse(paths.active.exists())
        records = list(paths.transfers.glob("*.json"))
        self.assertEqual(len(records), 1)
        orphan = json.loads(records[0].read_text(encoding="utf-8"))
        recovered = transfer_control.prepare(
            self.repo,
            source_session_id=self.SOURCE,
            goal_identity=self.GOAL,
            capsule_path=str(self.capsule),
            capsule_revision=1,
            capsule_sha256=self.sha,
            resume_ready=True,
            next_action=self.NEXT_ACTION,
            validation_evidence=[],
            resume_validation_command=self.VALIDATION_COMMAND,
            resume_validation_expected=self.VALIDATION_EXPECTED,
            nonce="differentnonceabcdefghijklmnop",
        )
        self.assertTrue(recovered["idempotent"])
        self.assertEqual(recovered["transfer_id"], orphan["transfer_id"])
        self.assertTrue(paths.active.exists())

    def test_prepare_rejects_multiple_or_conflicting_orphans(self) -> None:
        with mock.patch.dict(
            os.environ,
            {transfer_control.FAULT_ENV: "after_prepare_record_before_pointer"},
        ):
            with self.assertRaises(transfer_control.FaultInjected):
                self.prepare()
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        original_path = next(paths.transfers.glob("*.json"))
        duplicate = json.loads(original_path.read_text(encoding="utf-8"))
        duplicate["nonce"] = "duplicateorphanabcdefghijklmnop"
        duplicate["transfer_id"] = transfer_control._transfer_id(1, duplicate["nonce"])
        transfer_control.durable_write_json(
            transfer_control._record_path(paths, duplicate["transfer_id"]), duplicate
        )
        with self.assertRaises(transfer_control.TransferError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, "orphan_conflict")

    def test_cli_emits_json_and_persists_durable_state(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo",
                str(self.repo),
                "prepare",
                "--source-session-id",
                self.SOURCE,
                "--goal-identity",
                self.GOAL,
                "--capsule-path",
                str(self.capsule),
                "--capsule-revision",
                "1",
                "--capsule-sha256",
                self.sha,
                "--resume-ready",
                "--next-action",
                self.NEXT_ACTION,
                "--resume-validation-command",
                self.VALIDATION_COMMAND,
                "--resume-validation-expected",
                self.VALIDATION_EXPECTED,
                "--nonce",
                self.NONCE,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        paths = transfer_control.transfer_paths(self.repo, self.SOURCE)
        persisted = json.loads(paths.active.read_text(encoding="utf-8"))
        self.assertEqual(persisted["transfer_id"], payload["transfer_id"])
        self.assertEqual(persisted["durability"]["readback_verified"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
