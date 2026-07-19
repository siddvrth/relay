#!/usr/bin/env python3
"""Regression tests for the structured release-readiness report."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


MODULE_PATH = Path(__file__).with_name("check_release_readiness.py")
PROJECT_ROOT = MODULE_PATH.parents[1]
SPEC = importlib.util.spec_from_file_location("check_release_readiness", MODULE_PATH)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
DIST_MODULE_PATH = Path(__file__).with_name("validate_distribution.py")
DIST_SPEC = importlib.util.spec_from_file_location("validate_distribution", DIST_MODULE_PATH)
assert DIST_SPEC and DIST_SPEC.loader
distribution = importlib.util.module_from_spec(DIST_SPEC)
DIST_SPEC.loader.exec_module(distribution)


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr="")


def v2_study_document() -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for pair in range(1, 21):
        for condition, tokens in (
            ("current_canonical", 200 + pair),
            ("candidate", 100 + pair),
        ):
            rows.append(
                {
                    "paired_run_id": f"pair-{pair:02d}",
                    "task_id": f"task-{pair:02d}",
                    "condition": condition,
                    "run_started_at": "2026-07-02T00:00:00+00:00",
                    "tokensUsed": tokens,
                    "outcome": "passed",
                    "resume_ready": True,
                    "quality": {
                        "task_correctness": 2,
                        "constraint_adherence": 2,
                        "completeness": 2,
                        "validation_evidence": 2,
                        "resume_usefulness": 2,
                    },
                    "rubric_notes": "Blinded fixture score.",
                    "capsule_bytes": 1200,
                    "prompt_bytes": 300,
                }
            )
    return {
        "schema_version": 2,
        "study_type": "token_efficient_fresh_handoff_v2",
        "telemetry_scope": "exact_goal_period_tokensUsed",
        "control_condition": "current_canonical",
        "candidate_condition": "candidate",
        "control_commit_id": "1" * 40,
        "candidate_commit_id": "2" * 40,
        "control_runtime_sha256": "3" * 64,
        "candidate_runtime_sha256": "4" * 64,
        "model": "test-model",
        "reasoning_effort": "fixed",
        "repository": "fixture/repository",
        "goal_token_budget": 5000,
        "task_set_id": "frozen-task-set-v1",
        "randomization_plan": "predeclared alternating arm order",
        "sign_test_alpha": 0.05,
        "preregistration": {
            "frozen_at": "2026-07-01T00:00:00Z",
            "task_set_path": "artifacts/preregistration/tasks.json",
            "task_set_sha256": "5" * 64,
            "randomization_plan_path": "artifacts/preregistration/randomization.json",
            "randomization_plan_sha256": "6" * 64,
            "rubric_path": "artifacts/preregistration/rubric.json",
            "rubric_sha256": "7" * 64,
            "analysis_plan_path": "artifacts/preregistration/analysis.json",
            "analysis_plan_sha256": "8" * 64,
        },
        "rubric": {
            "task_correctness": 0.40,
            "constraint_adherence": 0.20,
            "completeness": 0.15,
            "validation_evidence": 0.15,
            "resume_usefulness": 0.10,
        },
        "rows": rows,
    }


def v3_study_document() -> dict[str, object]:
    document = v2_study_document()
    document["schema_version"] = 3
    document["study_type"] = "token_efficient_fresh_handoff_v3"
    document["telemetry_scope"] = "aggregate_source_destination_chain_tokensUsed"
    rows = document["rows"]
    assert isinstance(rows, list)
    continuation_quality = {
        "next_step_correct": True,
        "constraints_retained": True,
        "no_completed_work_repeated": True,
        "no_remaining_work_skipped": True,
        "repository_goal_reconciled": True,
        "validation_sufficient": True,
        "middle_critical_fact_recovered": True,
    }
    for row in rows:
        tokens_used = int(row["tokensUsed"])
        row.update(
            {
                "qualifies_as_claim_evidence": True,
                "source_tokens_before_handoff": 40,
                "handoff_generation_tokens": 10,
                "destination_resume_tokens": 20,
                "completion_tokens_after_resume": tokens_used - 70,
                "handoff_count": 1,
                "duplicated_work_action_count": 0,
                "post_ack_source_tokens": 0,
                "post_ack_source_actions": 0,
                "transfer_latency_ms": 100,
                "acknowledgement_outcome": "accepted",
                "acknowledgement_latency_ms": 50,
                "acknowledgement_attempt_count": 1,
                "acknowledgement_failure_count": 0,
                "source_stop_capability": "native_interrupt",
                "source_stop_outcome": "interrupted",
                "source_stop_latency_ms": 25,
                "source_stop_attempt_count": 1,
                "source_stop_failure_count": 0,
                "duplicate_destination_count": 0,
                "ownership_conflict_count": 0,
                "retry_count": 0,
                "termination_pending_observed": False,
                "termination_pending_recovered": False,
                "human_intervention_count": 0,
                "continuation_quality": dict(continuation_quality),
            }
        )
    return document


class ReleaseReadinessTests(unittest.TestCase):
    def assess(self, root: Path) -> dict[str, Any]:
        with mock.patch.object(
            release,
            "git",
            side_effect=[completed(), completed(stdout="https://example.com/fresh-handoff.git\n"), completed()],
        ):
            return release.assess(root)

    @staticmethod
    def write_live_evidence(
        root: Path,
        *,
        checked_at: str | None = None,
        hooks_json_sha256: str | None = None,
    ) -> Path:
        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        adapters = (
            "hooks/checkpoint_and_continue_hook.sh",
            "codex/checkpoint_and_continue_hook.sh",
            "scripts/workflow/checkpoint_and_continue_hook.sh",
        )
        manifest = json.loads(
            (root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        value = {
            "schema_version": 1,
            "evidence_type": "codex_live_hooks_trust",
            "checked_via": "/hooks",
            "checked_at": checked_at
            or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "loaded": True,
            "trusted": True,
            "hook_events": ["UserPromptSubmit", "PreToolUse", "PreCompact", "Stop"],
            "plugin_version": manifest["version"],
            "hooks_json_sha256": hooks_json_sha256
            or digest(root / "hooks" / "hooks.json"),
            "adapter_sha256s": {
                adapter: digest(root / adapter) for adapter in adapters
            },
        }
        path = root / "artifacts" / "metrics" / "live-hooks-trust.json"
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def git(root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def make_bound_release(
        self,
        temp: str,
        *,
        telemetry_version: int = 2,
    ) -> tuple[Path, Path, dict[str, object]]:
        root = Path(temp) / "release"
        shutil.copytree(
            PROJECT_ROOT,
            root,
            ignore=shutil.ignore_patterns(
                ".git", ".agents", ".omo", ".omx", ".DS_Store", "__pycache__"
            ),
        )
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "Release Test")
        self.git(root, "config", "user.email", "release-test@example.invalid")
        self.git(root, "remote", "add", "origin", "https://example.invalid/fresh-handoff.git")
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "control runtime")
        control = self.git(root, "rev-parse", "HEAD")

        preregistration = root / "artifacts" / "preregistration"
        preregistration.mkdir(parents=True, exist_ok=True)
        artifact_contents = {
            "tasks.json": b'{"tasks":["task-01"]}\n',
            "randomization.json": b'{"order":"alternating"}\n',
            "rubric.json": b'{"scale":[0,1,2]}\n',
            "analysis.json": b'{"test":"exact-sign"}\n',
        }
        for name, content in artifact_contents.items():
            (preregistration / name).write_bytes(content)
        runtime = root / "skills" / "checkpoint-and-continue" / "scripts" / "write_handoff.py"
        runtime.write_text(
            runtime.read_text(encoding="utf-8") + "\n# candidate-runtime-fixture\n",
            encoding="utf-8",
        )
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "candidate runtime and frozen preregistration")
        candidate = self.git(root, "rev-parse", "HEAD")

        document = v3_study_document() if telemetry_version == 3 else v2_study_document()
        document["repository"] = "https://example.invalid/fresh-handoff.git"
        document["control_commit_id"] = control
        document["candidate_commit_id"] = candidate
        document["control_runtime_sha256"] = release.runtime_digest_at_commit(root, control)
        document["candidate_runtime_sha256"] = release.runtime_digest_at_commit(root, candidate)
        prereg = document["preregistration"]
        assert isinstance(prereg, dict)
        for stem, filename in (
            ("task_set", "tasks.json"),
            ("randomization_plan", "randomization.json"),
            ("rubric", "rubric.json"),
            ("analysis_plan", "analysis.json"),
        ):
            content = artifact_contents[filename]
            prereg[f"{stem}_sha256"] = hashlib.sha256(content).hexdigest()

        evidence = root / "artifacts" / "metrics" / f"v{telemetry_version}-paired-study.json"
        evidence.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.git(root, "add", str(evidence.relative_to(root)))
        self.git(root, "commit", "-qm", "evidence-only release commit")
        return root, evidence, document

    def commit_evidence(self, root: Path, evidence: Path, document: dict[str, object]) -> None:
        evidence.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.git(root, "add", str(evidence.relative_to(root)))
        self.git(root, "commit", "-qm", "alter evidence fixture")

    def test_missing_manifest_is_a_structured_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self.assess(Path(temp))
        self.assertIn("plugin manifest is missing", result["blockers"])

    def test_v2_push_validation_is_required(self) -> None:
        error = release.assess_ci_validation_workflow(PROJECT_ROOT)

        self.assertIsNone(error)

    def test_workflow_without_v2_push_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflow = root / ".github" / "workflows" / "validate.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: validate\n\n"
                "on:\n"
                "  push:\n"
                "    branches:\n"
                "      - main\n",
                encoding="utf-8",
            )

            result = self.assess(root)

        self.assertIn(
            "CI validation workflow must run on pushes to exactly main and v2",
            result["blockers"],
        )

    def test_malformed_manifest_is_a_structured_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{broken", encoding="utf-8")
            result = self.assess(root)
        self.assertTrue(
            any(str(item).startswith("plugin manifest is invalid JSON:") for item in result["blockers"])
        )

    def test_valid_manifest_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / ".codex-plugin" / "plugin.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "name": "fresh-handoff",
                        "repository": "https://github.com/siddvrth/fresh-handoff",
                        "license": "MIT",
                    }
                ),
                encoding="utf-8",
            )
            result = self.assess(root)
        self.assertNotIn("plugin manifest is missing", result["blockers"])
        self.assertFalse(
            any(str(item).startswith("plugin manifest is invalid JSON:") for item in result["blockers"])
        )

    def test_clean_committed_release_with_license_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            shutil.copytree(
                PROJECT_ROOT,
                root,
                ignore=shutil.ignore_patterns(
                    ".git", ".agents", ".omo", ".omx", ".DS_Store", "__pycache__"
                ),
            )
            manifest_path = root / ".codex-plugin" / "plugin.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["license"] = "TEST-ONLY"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            (root / "LICENSE").write_text("Test-only release gate fixture.\n", encoding="utf-8")

            for command in (
                ("init", "-q"),
                ("config", "user.name", "Release Test"),
                ("config", "user.email", "release-test@example.invalid"),
                ("add", "."),
                ("commit", "-qm", "test release"),
                ("remote", "add", "origin", "https://github.com/siddvrth/fresh-handoff.git"),
            ):
                subprocess.run(["git", *command], cwd=root, check=True, capture_output=True, text=True)

            result = release.assess(root)

        self.assertTrue(result["ready"], result["blockers"])
        self.assertEqual([], result["blockers"])
        self.assertEqual(result["release_mode"], "experimental_non_claim")
        self.assertFalse(result["token_efficiency_claim_ready"])
        self.assertFalse(result["cost_claim_ready"])
        self.assertTrue(
            any("v3 acknowledgement-gated evidence" in item for item in result["claim_blockers"])
        )
        self.assertTrue(
            any("/hooks" in item for item in result["claim_blockers"])
        )

    def test_bound_v2_is_prior_schema_and_cannot_unlock_acknowledgement_gated_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, _ = self.make_bound_release(temp)
            result = release.assess(root)
            self.assertFalse(result["empirical_evidence"]["gate_passed"])
            self.assertEqual(result["empirical_evidence"]["valid_v2_study_count"], 1)
            self.assertFalse(result["token_efficiency_claim_ready"])
            self.assertTrue(any("V2 is prior-schema only" in item for item in result["claim_blockers"]))

            live_evidence = self.write_live_evidence(root)
            self.git(root, "add", str(live_evidence.relative_to(root)))
            self.git(root, "commit", "-qm", "live hook evidence-only commit")
            trusted = release.assess(root)

        self.assertFalse(trusted["token_efficiency_claim_ready"])
        self.assertFalse(trusted["cost_claim_ready"])
        self.assertTrue(
            any("V2 is prior-schema only" in item for item in trusted["claim_blockers"])
        )

    def test_real_git_bound_v2_is_retained_as_verified_prior_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, _ = self.make_bound_release(temp)
            result = release.assess_empirical_evidence(root)

        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["valid_v2_study_count"], 1)
        bindings = result["v2_verified_bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertNotEqual(bindings[0]["candidate_commit"], bindings[0]["release_head"])
        self.assertEqual(
            bindings[0]["candidate_runtime_sha256"],
            bindings[0]["release_runtime_sha256"],
        )
        self.assertIn(
            "skills/checkpoint-and-continue/scripts/transfer_control.py",
            release.RUNTIME_BINDING_PATHS,
        )

    def test_real_git_bound_v3_allows_evidence_only_release_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, _ = self.make_bound_release(temp, telemetry_version=3)
            result = release.assess_empirical_evidence(root)

        self.assertTrue(result["gate_passed"], result["errors"])
        self.assertEqual(result["valid_passing_v3_study_count"], 1)
        bindings = result["verified_bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertNotEqual(bindings[0]["candidate_commit"], bindings[0]["release_head"])
        self.assertEqual(
            bindings[0]["candidate_runtime_sha256"],
            bindings[0]["release_runtime_sha256"],
        )
        for path in (
            "skills/checkpoint-and-continue/scripts/goal_telemetry_v3.py",
            "skills/checkpoint-and-continue/scripts/goal_telemetry_v3_contract.py",
            "skills/checkpoint-and-continue/scripts/goal_telemetry_v3_report.py",
            "skills/checkpoint-and-continue/scripts/goal_telemetry_v3_schema.py",
        ):
            self.assertIn(path, release.RUNTIME_BINDING_PATHS)

    def test_bound_passing_v3_plus_live_hooks_unlocks_token_claim_readiness_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, _ = self.make_bound_release(temp, telemetry_version=3)
            live_evidence = self.write_live_evidence(root)
            self.git(root, "add", str(live_evidence.relative_to(root)))
            self.git(root, "commit", "-qm", "live hook evidence-only commit")
            result = release.assess(root)

        self.assertTrue(result["token_efficiency_claim_ready"], result["claim_blockers"])
        self.assertFalse(result["cost_claim_ready"])
        self.assertEqual(result["release_mode"], "experimental_non_claim")

    def test_v3_diagnostic_conditions_cannot_unlock_claim_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, evidence, document = self.make_bound_release(temp, telemetry_version=3)
            rows = document["rows"]
            assert isinstance(rows, list)
            for row in rows:
                if row["condition"] == "candidate":
                    row["outcome"] = "failed"
                    row["resume_ready"] = False
            diagnostic = json.loads(json.dumps(rows[-1]))
            diagnostic.update(
                {
                    "paired_run_id": "diagnostic-fast-01",
                    "task_id": "diagnostic-fast-task-01",
                    "condition": "diagnostic_fast",
                    "qualifies_as_claim_evidence": False,
                    "tokensUsed": 70,
                    "completion_tokens_after_resume": 0,
                }
            )
            rows.append(diagnostic)
            self.commit_evidence(root, evidence, document)
            result = release.assess_empirical_evidence(root)

        self.assertFalse(result["gate_passed"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["valid_passing_v3_study_count"], 0)

    def test_cross_schema_or_partial_v3_markers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, evidence, document = self.make_bound_release(temp, telemetry_version=3)
            document["study_type"] = "token_efficient_fresh_handoff_v2"
            self.commit_evidence(root, evidence, document)
            result = release.assess_empirical_evidence(root)

        self.assertFalse(result["gate_passed"])
        self.assertTrue(any("mixed or partial" in error for error in result["errors"]))

    def test_distribution_accepts_v2_and_v3_and_allows_zero_v3_nonclaim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "distribution"
            shutil.copytree(PROJECT_ROOT, root, ignore=shutil.ignore_patterns(".git", ".omx"))
            baseline = distribution.validate_goal_telemetry_artifacts(root)
            self.assertEqual(baseline["v3_study_count"], 0)
            metrics = root / "artifacts" / "metrics"
            (metrics / "v2-fixture.json").write_text(
                json.dumps(v2_study_document()), encoding="utf-8"
            )
            (metrics / "v3-fixture.json").write_text(
                json.dumps(v3_study_document()), encoding="utf-8"
            )
            result = distribution.validate_goal_telemetry_artifacts(root)

        self.assertEqual(result["v2_study_count"], 1)
        self.assertEqual(result["v3_study_count"], 1)

    def test_five_pair_pilot_preregistration_is_frozen_and_complete(self) -> None:
        pilot = json.loads(
            (
                PROJECT_ROOT
                / "artifacts/metrics/fresh-handoff-v2-five-pair-pilot-preregistration.json"
            ).read_text(encoding="utf-8")
        )
        pairs = pilot["pairs"]
        expected_task_ids = [
            "revision-owner-concurrency",
            "validation-resume-schema",
            "tombstone-retry-recovery",
            "fresh-install-portability",
            "release-evidence-audit",
        ]
        expected_result_fields = {
            "status",
            "task_correctness",
            "constraint_retention",
            "completeness",
            "validation_quality",
            "resume_usefulness",
            "completed_work_repeated",
            "remaining_work_skipped",
            "first_resumed_action_correct",
            "human_intervention",
            "total_aggregate_goal_tokens",
            "source_tokens_before_handoff",
            "handoff_generation_tokens",
            "destination_resume_tokens",
            "completion_after_resume_tokens",
            "capsule_bytes",
            "prompt_bytes",
        }

        self.assertEqual([pair["task_id"] for pair in pairs], expected_task_ids)
        self.assertEqual([pair["arm_order"] for pair in pairs], ["AB", "BA", "AB", "BA", "AB"])
        self.assertEqual(len({pair["prompt"] for pair in pairs}), 5)
        self.assertEqual(len({pair["task_repository_commit"] for pair in pairs}), 1)
        self.assertEqual(
            pilot["runtime_bindings"]["A"]["commit_id"],
            "301ea7ccb7d0177f26d66c17680ffd5e6115a872",
        )
        self.assertEqual(pilot["runtime_bindings"]["B"]["commit_id"], "FINAL_HEAD")
        self.assertIn("replace FINAL_HEAD", pilot["runtime_bindings"]["B"]["binding_rule"])
        self.assertEqual(
            pilot["shared_run_configuration"],
            {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "goal_token_budget": 5000,
                "same_task_objective_per_pair": True,
                "same_repository_state_per_pair": True,
                "same_evaluation_rubric_per_pair": True,
            },
        )
        for pair in pairs:
            with self.subTest(task_id=pair["task_id"]):
                self.assertEqual(len(pair["arms"]), 2)
                self.assertEqual("".join(arm["arm"] for arm in pair["arms"]), pair["arm_order"])
                for arm in pair["arms"]:
                    result = arm["result"]
                    self.assertEqual(set(result), expected_result_fields)
                    self.assertEqual(result["status"], "not_run")
                    self.assertTrue(
                        all(value is None for key, value in result.items() if key != "status")
                    )
        self.assertFalse(pilot["claim_eligible"])
        self.assertEqual(pilot["status"], "not_run")

    def test_pilot_promising_gate_is_six_conjunctive_and_negative_stops(self) -> None:
        pilot = json.loads(
            (
                PROJECT_ROOT
                / "artifacts/metrics/fresh-handoff-v2-five-pair-pilot-preregistration.json"
            ).read_text(encoding="utf-8")
        )
        gates = pilot["analysis_plan"]["promising_gates"]

        self.assertEqual(len(gates), 6)
        self.assertTrue(all(gate["required"] for gate in gates))
        self.assertEqual(pilot["analysis_plan"]["combination"], "all_six_conjunctive")
        self.assertEqual(pilot["analysis_plan"]["negative_stop"]["action"], "stop")
        self.assertTrue(pilot["retention_policy"]["retain_failed_runs"])
        self.assertTrue(pilot["retention_policy"]["retain_unfavorable_runs"])

    def test_five_pair_pilot_is_nonclaim_and_cannot_unlock_release(self) -> None:
        limitations = json.loads(
            (
                PROJECT_ROOT
                / "artifacts/metrics/fresh-handoff-v2-pilot-environment-limitations.json"
            ).read_text(encoding="utf-8")
        )
        empirical = release.assess_empirical_evidence(PROJECT_ROOT)
        distribution_result = distribution.validate_goal_telemetry_artifacts(PROJECT_ROOT)
        release_result = release.assess(PROJECT_ROOT)

        self.assertEqual(limitations["formal_v3_minimum_pairs"], 20)
        self.assertEqual(len(limitations["capability_probes"]), 5)
        self.assertEqual(
            [probe["status"] for probe in limitations["capability_probes"]],
            ["executable", "not_executable", "not_executable", "not_executable", "not_executable"],
        )
        self.assertTrue(
            all(
                probe["result"] is not None
                and probe["status"] in {"executable", "not_executable"}
                for probe in limitations["capability_probes"]
            )
        )
        self.assertFalse(empirical["gate_passed"])
        self.assertNotIn("schema_version", limitations)
        self.assertNotIn("study_type", limitations)
        self.assertNotIn(
            "fresh-handoff-v2-five-pair-pilot-preregistration.json",
            empirical["candidate_files"],
        )
        self.assertNotIn(
            "fresh-handoff-v2-pilot-environment-limitations.json",
            empirical["candidate_files"],
        )
        self.assertEqual(distribution_result["v2_study_count"], 0)
        self.assertEqual(distribution_result["v3_study_count"], 0)
        self.assertFalse(release_result["token_efficiency_claim_ready"])
        self.assertFalse(release_result["cost_claim_ready"])

    def test_distribution_rejects_malformed_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "distribution"
            shutil.copytree(PROJECT_ROOT, root, ignore=shutil.ignore_patterns(".git", ".omx"))
            document = v3_study_document()
            rows = document["rows"]
            assert isinstance(rows, list)
            rows[0]["completion_tokens_after_resume"] = 0
            (root / "artifacts" / "metrics" / "v3-malformed.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "invalid v3 goal telemetry"):
                distribution.validate_goal_telemetry_artifacts(root)

    def test_git_binding_rejects_guidance_content_drift(self) -> None:
        guidance_paths = (
            "skills/checkpoint-and-continue/SKILL.md",
            "skills/checkpoint-and-continue/reference.md",
            "skills/checkpoint-and-continue/examples.md",
            "skills/checkpoint-and-continue/agents/openai.yaml",
        )
        for guidance in guidance_paths:
            with self.subTest(guidance=guidance), tempfile.TemporaryDirectory() as temp:
                root, _, _ = self.make_bound_release(temp)
                path = root / guidance
                path.write_text(
                    path.read_text(encoding="utf-8") + "\n# post-candidate drift\n",
                    encoding="utf-8",
                )
                self.git(root, "add", guidance)
                self.git(root, "commit", "-qm", "guidance content drift")

                result = release.assess_empirical_evidence(root)

                self.assertFalse(result["gate_passed"])
                self.assertTrue(
                    any("runtime has drifted" in str(error) for error in result["errors"]),
                    result["errors"],
                )

    def test_git_binding_rejects_guidance_mode_only_drift(self) -> None:
        guidance_paths = (
            "skills/checkpoint-and-continue/SKILL.md",
            "skills/checkpoint-and-continue/reference.md",
            "skills/checkpoint-and-continue/examples.md",
            "skills/checkpoint-and-continue/agents/openai.yaml",
        )
        for guidance in guidance_paths:
            with self.subTest(guidance=guidance), tempfile.TemporaryDirectory() as temp:
                root, _, _ = self.make_bound_release(temp)
                self.git(root, "update-index", "--chmod=+x", guidance)
                (root / guidance).chmod((root / guidance).stat().st_mode | 0o111)
                self.git(root, "commit", "-qm", "guidance mode-only drift")

                result = release.assess_empirical_evidence(root)

                self.assertFalse(result["gate_passed"])
                self.assertTrue(
                    any("runtime has drifted" in str(error) for error in result["errors"]),
                    result["errors"],
                )

    def test_git_binding_rejects_fabricated_ids_digests_paths_and_timestamps(self) -> None:
        for label in ("commit", "digest", "path", "timestamp"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root, evidence, document = self.make_bound_release(temp)
                if label == "commit":
                    document["candidate_commit_id"] = "f" * 40
                elif label == "digest":
                    document["candidate_runtime_sha256"] = "0" * 64
                elif label == "path":
                    preregistration = document["preregistration"]
                    assert isinstance(preregistration, dict)
                    preregistration["task_set_path"] = "../tasks.json"
                else:
                    rows = document["rows"]
                    assert isinstance(rows, list)
                    rows[0]["run_started_at"] = "2026-06-30T23:59:59Z"
                self.commit_evidence(root, evidence, document)
                result = release.assess_empirical_evidence(root)
                self.assertFalse(result["gate_passed"])
                self.assertTrue(result["errors"])

    def test_claiming_release_promotes_missing_evidence_to_release_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            shutil.copytree(
                PROJECT_ROOT,
                root,
                ignore=shutil.ignore_patterns(".git", ".agents", ".omx", ".DS_Store", "__pycache__"),
            )
            (root / "README.md").write_text(
                "# Fresh Handoff\n\nDo not claim token or cost improvement. "
                "Fresh Handoff saves goal tokens and lowers costs.\n",
                encoding="utf-8",
            )
            result = self.assess(root)

        self.assertEqual(result["release_mode"], "claiming_or_unspecified")
        self.assertFalse(result["ready"])
        self.assertTrue(
            any("token/cost claim blocked" in item for item in result["blockers"])
        )

    def test_positive_claim_in_plugin_manifest_string_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            shutil.copytree(
                PROJECT_ROOT,
                root,
                ignore=shutil.ignore_patterns(
                    ".git", ".agents", ".omx", ".DS_Store", "__pycache__"
                ),
            )
            manifest_path = root / ".codex-plugin" / "plugin.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["description"] = (
                "Do not claim token or cost improvement. "
                "Fresh Handoff saves goal tokens without sacrificing quality "
                "and lowers costs."
            )
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            result = self.assess(root)

        self.assertEqual(result["release_mode"], "claiming_or_unspecified")
        self.assertFalse(result["ready"])
        self.assertTrue(
            any(
                str(item).startswith(".codex-plugin/plugin.json:")
                for item in result["positive_claims"]
            )
        )

    def test_positive_claim_in_each_shipped_skill_markdown_surface_is_detected(self) -> None:
        for relative in (
            "skills/checkpoint-and-continue/SKILL.md",
            "skills/checkpoint-and-continue/reference.md",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                root = Path(temp) / "release"
                shutil.copytree(
                    PROJECT_ROOT,
                    root,
                    ignore=shutil.ignore_patterns(
                        ".git", ".agents", ".omx", ".DS_Store", "__pycache__"
                    ),
                )
                path = root / relative
                path.write_text(
                    "Do not claim token or cost improvement.\n\n"
                    "Fresh Handoff saves goal tokens and lowers costs.\n",
                    encoding="utf-8",
                )
                result = self.assess(root)

                self.assertEqual(
                    result["release_mode"],
                    "claiming_or_unspecified",
                )
                self.assertFalse(result["ready"])
                self.assertTrue(
                    any(
                        str(item).startswith(f"{relative}:")
                        for item in result["positive_claims"]
                    )
                )

    def test_direct_token_efficiency_claim_is_detected_at_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                "# Fresh Handoff\n\n"
                "Fresh Handoff improves token efficiency while retaining quality.\n",
                encoding="utf-8",
            )

            findings = release.scan_positive_public_claims(root)

        self.assertEqual(["README.md:3"], findings)

    def test_multiline_goal_token_claim_is_detected_at_verb_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                "# Fresh Handoff\n\n"
                "Fresh Handoff reduces\n"
                "goal tokens while retaining quality.\n",
                encoding="utf-8",
            )

            findings = release.scan_positive_public_claims(root)

        self.assertEqual(["README.md:3"], findings)

    def test_positive_claim_in_shipped_agent_yaml_is_detected_at_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "skills" / "checkpoint-and-continue" / "agents" / "openai.yaml"
            agent.parent.mkdir(parents=True)
            agent.write_text(
                "interface:\n"
                "  display_name: Fresh Handoff\n"
                "  description: Fresh Handoff improves token efficiency.\n",
                encoding="utf-8",
            )

            findings = release.scan_positive_public_claims(root)

        self.assertEqual(
            ["skills/checkpoint-and-continue/agents/openai.yaml:3"],
            findings,
        )

    def test_positive_claim_in_shipped_workflow_yaml_is_detected_at_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflow = root / ".github" / "workflows" / "validate.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "name: Validate\n"
                "description: Fresh Handoff provides better token efficiency.\n",
                encoding="utf-8",
            )

            findings = release.scan_positive_public_claims(root)

        self.assertEqual([".github/workflows/validate.yml:2"], findings)

    def test_later_positive_claim_is_not_hidden_by_earlier_negation(self) -> None:
        claims = (
            "Fresh Handoff does not lower costs and improves token efficiency.",
            "Although Fresh Handoff does not lower costs, Fresh Handoff improves "
            "token efficiency.",
            "Although Fresh Handoff does not lower costs, Fresh Handoff is more "
            "token-efficient than the baseline.",
            "Fresh Handoff does not lower costs while it improves token efficiency.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(release._contains_positive_claim(text))

    def test_later_positive_claim_is_not_hidden_by_historical_failure(self) -> None:
        claims = (
            "The historical study failed and Fresh Handoff improves token efficiency.",
            "The historical pre-v2 study failed and the candidate reduces goal tokens.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(release._contains_positive_claim(text))

    def test_explicit_local_subject_ends_prior_exclusion_scope(self) -> None:
        claims = (
            "This is not evidence that Fresh Handoff reduces goal tokens and the "
            "candidate reduces goal tokens.",
            "A paired study must pass before Fresh Handoff reduces goal tokens and "
            "the candidate reduces goal tokens.",
            "A claim that Fresh Handoff reduces token use is allowed only after "
            "evidence and the candidate reduces goal tokens.",
            "A historical claim that Fresh Handoff reduces token use failed and the "
            "candidate reduces goal tokens.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(release._contains_positive_claim(text))

    def test_comparative_token_efficiency_claim_is_detected(self) -> None:
        claims = (
            "Fresh Handoff is more token-efficient than the baseline.",
            "Fresh Handoff provides better token efficiency.",
            "Fresh Handoff provides greater token efficiency.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(release._contains_positive_claim(text))

    def test_present_and_past_positive_claim_verbs_are_detected(self) -> None:
        claims = (
            "Fresh Handoff saves goal tokens.",
            "Fresh Handoff saved goal tokens.",
            "Fresh Handoff reduces goal tokens.",
            "Fresh Handoff reduced goal tokens.",
            "Fresh Handoff lowers costs.",
            "Fresh Handoff lowered costs.",
            "Fresh Handoff improves token efficiency.",
            "Fresh Handoff improved token efficiency.",
            "Fresh Handoff boosts token efficiency.",
            "Fresh Handoff boosted token efficiency.",
            "Fresh Handoff enhances token efficiency.",
            "Fresh Handoff enhanced token efficiency.",
            "Fresh Handoff uses fewer tokens.",
            "Fresh Handoff used fewer tokens.",
            "Fresh Handoff consumes fewer tokens.",
            "Fresh Handoff consumed fewer tokens.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(release._contains_positive_claim(text))

    def test_bounded_efficiency_and_cost_vocabulary_is_detected(self) -> None:
        claims = (
            "Fresh Handoff requires fewer tokens.",
            "Fresh Handoff required fewer tokens.",
            "Fresh Handoff needs fewer tokens.",
            "Fresh Handoff needed fewer tokens.",
            "Fresh Handoff has higher token efficiency.",
            "Fresh Handoff and its adapters have better token efficiency.",
            "Fresh Handoff had higher token efficiency.",
            "Fresh Handoff is cheaper than the baseline.",
            "Fresh Handoff was cheaper than the baseline.",
            "Fresh tasks are cheaper than the baseline.",
            "Fresh tasks were cheaper than the baseline.",
            "Fresh Handoff costs less than the baseline.",
            "Fresh tasks cost less than the baseline.",
            "Fresh Handoff trims token usage.",
            "Fresh Handoff trimmed token usage.",
            "Fresh Handoff minimizes token usage.",
            "Fresh Handoff minimized token usage.",
            "Fresh Handoff yields lower token usage.",
            "Fresh Handoff yielded lower token usage.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(release._contains_positive_claim(text))

    def test_bounded_vocabulary_preserves_negative_and_conditional_controls(self) -> None:
        nonclaims = (
            "Fresh Handoff does not require fewer tokens.",
            "Fresh Handoff is not cheaper than the baseline.",
            "This is not evidence that Fresh Handoff trims token usage.",
            "A claim that Fresh Handoff minimizes token usage is allowed only after "
            "evidence.",
        )
        for text in nonclaims:
            with self.subTest(text=text):
                self.assertFalse(release._contains_positive_claim(text))

    def test_leading_evidence_gate_modal_claims_are_nonclaims(self) -> None:
        nonclaims = (
            "If paired evidence passes, Fresh Handoff may reduce token usage.",
            "When the study succeeds, Fresh Handoff can improve token efficiency.",
            "Unless the test fails, Fresh Handoff could lower token costs.",
            "If paired evidence passes,\nFresh Handoff may reduce token usage and "
            "improve token efficiency.",
            "If paired evidence passes, Fresh Handoff may reduce token usage and may "
            "improve token efficiency.",
            "When the study succeeds, Fresh Handoff can reduce token usage or could "
            "lower token costs.",
            "Unless the test fails, Fresh Handoff MAY reduce token usage AND CAN "
            "improve token efficiency OR COULD lower token costs.",
            "If paired evidence passes, Fresh Handoff may reduce token usage\nand may "
            "improve token efficiency.",
            "If evidence passes, Fresh Handoff may reduce token usage, and may "
            "improve token efficiency.",
        )
        for text in nonclaims:
            with self.subTest(text=text):
                self.assertFalse(release._contains_positive_claim(text))

    def test_leading_evidence_gate_does_not_hide_independent_claims(self) -> None:
        claims = (
            "Fresh Handoff may reduce token usage.",
            "If paired evidence passes, Fresh Handoff reduces token usage.",
            "If paired evidence passes, measurements are recorded, Fresh Handoff "
            "may reduce token usage.",
            "If paired evidence passes, Fresh Handoff may reduce token usage. Fresh "
            "Handoff improves token efficiency.",
            "If paired evidence passes, Fresh Handoff may reduce token usage, but "
            "Fresh Handoff improves token efficiency.",
            "If paired evidence passes, Fresh Handoff may reduce token usage and the "
            "candidate improves token efficiency.",
            "Gift evidence passes, Fresh Handoff may reduce token usage.",
            "Whenever evidence passes, Fresh Handoff may reduce token usage.",
            "If paired evidence passes, Fresh Handoff may reduce token usage and the "
            "tool may improve token efficiency.",
            "If paired evidence passes, Fresh Handoff may reduce token usage and also "
            "may improve token efficiency.",
            "If paired evidence passes, Fresh Handoff may reduce token usage and may "
            "later improve token efficiency.",
            "If paired evidence passes, Fresh Handoff may reduce token usage and, may "
            "improve token efficiency.",
            "If paired evidence passes, Fresh Handoff reduces token usage and may "
            "improve token efficiency.",
            "Although paired evidence passes, Fresh Handoff may reduce token usage and "
            "may improve token efficiency.",
            "If evidence passes, Fresh Handoff may reduce token usage: Fresh Handoff "
            "improves token efficiency.",
            "If evidence passes, Fresh Handoff may reduce token usage — Fresh Handoff "
            "improves token efficiency.",
            "If evidence passes, Fresh Handoff may reduce token usage – Fresh Handoff "
            "improves token efficiency.",
        )
        for text in claims:
            with self.subTest(text=text):
                self.assertTrue(release._contains_positive_claim(text))

    def test_hard_boundaries_and_trailing_metadata_cannot_hide_claims(self) -> None:
        boundary_claims = (
            "Fresh Handoff does not reduce token usage.Fresh Handoff improves token "
            "efficiency.",
            "Fresh Handoff does not reduce token usage.”Fresh Handoff improves token "
            "efficiency.",
            "If evidence passes, Fresh Handoff may reduce token usage:Fresh Handoff "
            "improves token efficiency.",
            "Fresh Handoff does not reduce token usage.“Fresh Handoff improves token "
            "efficiency.”",
            "Fresh Handoff does not reduce token usage.‘Fresh Handoff improves token "
            "efficiency.’",
            "Fresh Handoff does not reduce token usage.»Fresh Handoff improves token "
            "efficiency.",
            "Fresh Handoff does not reduce token usage.›Fresh Handoff improves token "
            "efficiency.",
            "Fresh Handoff does not lower costs though Fresh Handoff improves token "
            "efficiency.",
            "Fresh Handoff does not lower costs even though Fresh Handoff improves "
            "token efficiency.",
            "The historical study failed though Fresh Handoff improves token "
            "efficiency.",
            "The historical study failed even though Fresh Handoff improves token "
            "efficiency.",
        )
        for text in boundary_claims:
            with self.subTest(text=text):
                self.assertIn(text.index("improves"), tuple(release._claim_offsets(text)))

        suffix_claims = (
            "Fresh Handoff reduces tokens and positive paired differences mean "
            "candidate minus control.",
            "Fresh Handoff reduces tokens and the claim is allowed only after evidence.",
            "Fresh Handoff reduces tokens and the historical baseline is documented.",
            "Fresh Handoff reduces tokens (positive paired differences mean candidate "
            "minus control).",
            "Fresh Handoff reduces tokens (the claim is allowed only after evidence).",
            "Fresh Handoff reduces tokens (historical baseline, only 8 of 20).",
            "Fresh Handoff reduces tokens (not evidence of lower costs).",
            "Historical analysis shows Fresh Handoff reduces tokens.",
        )
        for text in suffix_claims:
            with self.subTest(text=text):
                self.assertIn(text.index("reduces"), tuple(release._claim_offsets(text)))

        self.assertFalse(
            release._contains_positive_claim(
                "Version 2.0 does not prove Fresh Handoff reduces goal tokens."
            )
        )
        self.assertEqual(
            [],
            list(release.CLAUSE_BOUNDARY_PATTERN.finditer("Version 2.0")),
        )

    def test_suffix_qualifiers_keep_claims_nonclaiming(self) -> None:
        nonclaims = (
            "A claim that Fresh Handoff reduces token use is allowed only after evidence.",
            "A historical claim that Fresh Handoff reduces token use failed.",
            "A historical assertion that Fresh Handoff improves token efficiency was "
            "a negative result.",
            "A historical claim that Fresh Handoff reduces token use though it failed.",
            "A historical assertion that Fresh Handoff improves token efficiency even "
            "though it was a negative result.",
        )
        for text in nonclaims:
            with self.subTest(text=text):
                self.assertFalse(release._contains_positive_claim(text))

    def test_metric_definitions_and_numeric_result_rows_are_not_claims(self) -> None:
        self.assertFalse(
            release._contains_positive_claim(
                "Positive differences below mean the clean task used fewer goal tokens."
            )
        )
        self.assertFalse(
            release._contains_positive_claim(
                "The retained dataset contains historical pre-v2 pairs. Clean tasks "
                "shed inherited turns, but they used fewer goal tokens in only 8 of "
                "20 pairs."
            )
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                "| Result | Exact value |\n"
                "| --- | ---: |\n"
                "| Clean used fewer tokens | 8 |\n",
                encoding="utf-8",
            )

            findings = release.scan_positive_public_claims(root)

        self.assertEqual([], findings)

    def test_claim_classifier_preserves_nonclaim_contexts(self) -> None:
        nonclaims = (
            "Fresh Handoff does not reduce goal tokens.",
            "Fresh Handoff does not lower costs and does not improve token efficiency.",
            "Fresh Handoff does not lower costs or improve token efficiency.",
            "Fresh Handoff does not save tokens, lower costs, or improve token efficiency.",
            "Fresh Handoff never saves tokens or lowers costs.",
            "This is not evidence that Fresh Handoff lowers costs and improves "
            "token efficiency.",
            "A paired study must pass before Fresh Handoff reduces goal tokens.",
            "A paired study must pass before Fresh Handoff reduces goal tokens and "
            "improves token efficiency.",
            "This is not evidence that Fresh Handoff reduces goal tokens and "
            "improves token efficiency.",
            "The historical pre-v2 study failed and is not evidence that Fresh "
            "Handoff improves token efficiency.",
        )
        for text in nonclaims:
            with self.subTest(text=text):
                self.assertFalse(release._contains_positive_claim(text))

    def test_release_policy_must_be_exact_machine_readable_nonclaim(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            shutil.copytree(
                PROJECT_ROOT,
                root,
                ignore=shutil.ignore_patterns(
                    ".git", ".agents", ".omx", ".DS_Store", "__pycache__"
                ),
            )
            policy = root / ".codex-plugin" / "release-policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "release_mode": "experimental_non_claim",
                        "token_efficiency_claim": False,
                        "cost_savings_claim": False,
                        "unexpected": True,
                    }
                ),
                encoding="utf-8",
            )
            result = self.assess(root)

        self.assertEqual(result["release_mode"], "claiming_or_unspecified")
        self.assertFalse(result["ready"])
        self.assertTrue(
            any("release policy" in item for item in result["blockers"])
        )

    def test_live_hooks_evidence_rejects_stale_or_unbound_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            shutil.copytree(
                PROJECT_ROOT,
                root,
                ignore=shutil.ignore_patterns(
                    ".git", ".agents", ".omx", ".DS_Store", "__pycache__"
                ),
            )
            stale = (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
            ).isoformat().replace("+00:00", "Z")
            self.write_live_evidence(root, checked_at=stale)
            stale_result = release.assess_live_hooks_trust(root)
            self.assertFalse(stale_result["ready"])

            self.write_live_evidence(root, hooks_json_sha256="0" * 64)
            unbound_result = release.assess_live_hooks_trust(root)
            self.assertFalse(unbound_result["ready"])

            self.write_live_evidence(root)
            bound_result = release.assess_live_hooks_trust(root)
            self.assertTrue(bound_result["ready"], bound_result["error"])

            evidence = root / "artifacts/metrics/live-hooks-trust.json"
            value = json.loads(evidence.read_text(encoding="utf-8"))
            value["hook_events"].remove("PreToolUse")
            evidence.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            missing_guard = release.assess_live_hooks_trust(root)
            self.assertFalse(missing_guard["ready"])
            self.assertIn("all hooks loaded", str(missing_guard["error"]))


if __name__ == "__main__":
    test_argv = sys.argv
    if len(test_argv) >= 3 and test_argv[1] == "-k" and " or " in test_argv[2]:
        patterns = test_argv[2].split(" or ")
        test_argv = [
            test_argv[0],
            *(item for pattern in patterns for item in ("-k", pattern)),
            *test_argv[3:],
        ]
    unittest.main(argv=test_argv)
