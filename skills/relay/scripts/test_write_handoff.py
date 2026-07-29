#!/usr/bin/env python3
"""Lightweight tests for relay scripts (stdlib unittest only)."""

from __future__ import annotations

import json
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent


def resolve_layout() -> tuple[Path, Path | None]:
    if (
        SKILL_ROOT.name == "relay"
        and SKILL_ROOT.parent.name == "skills"
        and SKILL_ROOT.parent.parent.name == ".agents"
    ):
        repo = SKILL_ROOT.parents[2]
        candidates = (repo / "packages/relay", repo)
        package_root = next(
            (
                candidate
                for candidate in candidates
                if (candidate / "skills/relay").is_dir()
                and (candidate / "artifacts").is_dir()
            ),
            None,
        )
        return (
            repo,
            package_root,
        )

    if SKILL_ROOT.name == "relay" and SKILL_ROOT.parent.name == "skills":
        package_root = SKILL_ROOT.parents[1]
        return (
            package_root,
            package_root,
        )

    raise RuntimeError(f"unsupported relay test layout: {SKILL_ROOT}")


REPO, PACKAGE_ROOT = resolve_layout()
WRITE_HANDOFF = SCRIPTS / "write_handoff.py"
CONTEXT_HANDOFF = SCRIPTS / "context_handoff.py"
GOAL_TELEMETRY_REPORT = SCRIPTS / "goal_telemetry_report.py"
INSTALL = REPO / "install.sh"
AUDIT_INSTALL = REPO / "audit_install.sh"
PLUGIN_HOOK = REPO / "hooks/relay_hook.sh"
CODEX_HOOK = REPO / "codex/relay_hook.sh"
WORKFLOW_HOOK = REPO / "scripts/workflow/relay_hook.sh"
sys.path.insert(0, str(SCRIPTS))
from write_handoff import (  # noqa: E402
    build_continuation_prompt,
    default_out_path,
    session_scope,
    trim_lines,
    validate_structural_readiness,
)
import goal_telemetry_report as telemetry  # noqa: E402
from goal_telemetry_report import build_report  # noqa: E402


def run_write_handoff(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(WRITE_HANDOFF), *args]
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(command, text=True, capture_output=True, check=False, env=merged, cwd=REPO)


def run_context_handoff(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CONTEXT_HANDOFF), *args]
    return subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        cwd=REPO,
    )


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def json_payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    if result.returncode != 0:
        raise AssertionError(
            f"command failed with {result.returncode}:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"command did not emit JSON:\n{result.stdout}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def contract_value(payload: dict[str, object], *names: str) -> object | None:
    wanted = set(names)
    queue: list[object] = [payload]
    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            for key, value in current.items():
                if key in wanted:
                    return value
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(current, list):
            queue.extend(item for item in current if isinstance(item, (dict, list)))
    return None


def all_string_values(payload: object) -> list[str]:
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        values: list[str] = []
        for value in payload.values():
            values.extend(all_string_values(value))
        return values
    if isinstance(payload, list):
        values = []
        for value in payload:
            values.extend(all_string_values(value))
        return values
    return []


def normalize_transport_text(text: str, repo: Path) -> str:
    normalized = text.replace(str(repo), "<repo>")
    normalized = re.sub(
        r"Expected capsule SHA-256: [0-9a-f]{64}\.",
        "Expected capsule SHA-256: <sha256>.",
        normalized,
    )
    normalized = re.sub(
        r"Expected transfer nonce: [A-Za-z0-9_-]{22,128}\.",
        "Expected transfer nonce: <nonce>.",
        normalized,
    )
    normalized = re.sub(
        r"Expected transfer ID: r[1-9][0-9]*-[0-9a-f]{16}\.",
        "Expected transfer ID: <transfer-id>.",
        normalized,
    )
    normalized = re.sub(
        r"/sessions/[0-9a-f]{16}/",
        "/sessions/<scope>/",
        normalized,
    )
    return re.sub(
        r"\d{8}-\d{6}-\d{6}(?:-r\d+)?-handoff\.md",
        "<capsule>.md",
        normalized,
    )


def rich_handoff_args(repo: Path, *, session_id: str = "session-rich") -> list[str]:
    return [
        "--repo",
        str(repo),
        "--session-id",
        session_id,
        "--objective",
        "Ship the token-efficient relay package",
        "--active-task",
        "Lock the v2 capsule contract with deterministic tests",
        "--phase",
        "regression suite",
        "--status",
        "implementation in progress",
        "--completion-criteria",
        "Every required field round-trips and targeted tests pass",
        "--completed-work",
        "Read the approved PRD and test specification",
        "--remaining-work",
        "Implement the canonical runtime and run validation",
        "--constraints",
        "Use only the Python standard library; preserve legacy bytes",
        "--decisions",
        "UTF-8 byte budgets are authoritative",
        "--blockers",
        "Live hook trust still requires host validation",
        "--validation-status",
        "Regression suite added; runtime validation pending",
        "--resume-validation-command",
        "python3 focused_test.py",
        "--resume-validation-expected",
        "exit 0 and 7 tests pass",
        "--authoritative-files",
        "skills/relay/scripts/write_handoff.py::build_markdown",
        "--next-step",
        "Implement deterministic capsule budgeting in build_markdown",
        "--force-handoff",
        "--emit-json",
    ]


def replace_arg(args: list[str], flag: str, value: str) -> list[str]:
    replaced = list(args)
    index = replaced.index(flag)
    replaced[index + 1] = value
    return replaced


def remove_arg(args: list[str], flag: str, *, takes_value: bool = False) -> list[str]:
    trimmed = list(args)
    index = trimmed.index(flag)
    del trimmed[index : index + (2 if takes_value else 1)]
    return trimmed


def rich_context_args(repo: Path) -> list[str]:
    args = rich_handoff_args(repo)
    args.remove("--force-handoff")
    args.remove("--emit-json")
    return args


def run_hook(
    wrapper: Path,
    event: str,
    repo: Path,
    payload: dict[str, object],
    *,
    plugin_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ROOT"] = str(repo)
    env["RELAY_CODEX_APP_TRANSPORT"] = "disabled"
    if plugin_root is not None:
        env["PLUGIN_ROOT"] = str(plugin_root)
    return subprocess.run(
        ["bash", str(wrapper), event],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=repo,
        env=env,
    )


def install_runtime_fixture(repo: Path) -> None:
    target = repo / ".agents/skills/relay"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL_ROOT, target)


def seed_ready_state(repo: Path, session_id: str) -> dict[str, object]:
    args = rich_handoff_args(repo, session_id=session_id)
    return json_payload(run_write_handoff(*args, "--update-active-task-only"))


def find_latest_pointer(repo: Path) -> tuple[Path, dict[str, object]]:
    state_root = repo / ".omx/state/relay"
    for path in sorted(state_root.rglob("*")):
        if not path.is_file() or path.suffix in {".md", ".lock", ".tmp"}:
            continue
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(candidate, dict):
            continue
        keys = {str(key).lower() for key in candidate}
        if any("capsule" in key and "path" in key for key in keys) and any(
            "revision" in key for key in keys
        ):
            return path, candidate
    raise AssertionError(f"no metadata-only latest pointer found under {state_root}")


class WriteHandoffTests(unittest.TestCase):
    def test_beginning_middle_near_completion_without_filler(self) -> None:
        profiles = (
            (
                "beginning",
                ("--completed-work", "--decisions", "--blockers", "--validation-status"),
                ("completed_work", "decisions", "blockers", "validation_evidence"),
            ),
            (
                "middle",
                ("--blockers",),
                ("blockers",),
            ),
            (
                "near-completion",
                ("--decisions", "--blockers"),
                ("decisions", "blockers"),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            for label, omitted_flags, absent_fields in profiles:
                with self.subTest(label=label):
                    args = rich_handoff_args(repo, session_id=f"optional-{label}")
                    for flag in omitted_flags:
                        args = remove_arg(args, flag, takes_value=True)

                    payload = json_payload(run_write_handoff(*args))

                    self.assertIs(payload["resume_ready"], True)
                    capsule = Path(str(payload["capsule_path"]))
                    capsule_text = capsule.read_text(encoding="utf-8")
                    self.assertEqual(
                        hashlib.sha256(capsule.read_bytes()).hexdigest(),
                        payload["capsule_sha256"],
                    )
                    absent_line = "Absent optional state: " + ", ".join(absent_fields)
                    self.assertEqual(capsule_text.count("Absent optional state:"), 1)
                    self.assertIn(absent_line, capsule_text)
                    for field in absent_fields:
                        self.assertNotIn("TBD", capsule_text)
                        self.assertNotIn("not recorded", capsule_text.lower())

                    active_path = (
                        repo
                        / ".omx/state/relay/sessions"
                        / session_scope(f"optional-{label}")
                        / ".active-task.json"
                    )
                    active = json.loads(active_path.read_text(encoding="utf-8"))
                    for field in ("completed_work", "decisions", "blockers", "validation_evidence"):
                        self.assertIsInstance(active[field], list)
                        if field in absent_fields:
                            self.assertEqual(active[field], [])
                    self.assertNotIn("next_step", active)
                    self.assertIn("next_action", active)

                    prompt = str(payload["continuation_prompt"])
                    for value in (
                        payload["session_id"],
                        payload["revision"],
                        payload["capsule_sha256"],
                        payload["goal_identity"],
                        payload["transfer_nonce"],
                        payload["transfer_id"],
                        payload["next_action"],
                        payload["resume_validation"]["command"],
                        payload["resume_validation"]["expected"],
                    ):
                        self.assertIn(str(value), prompt)

    def test_critical_field_matrix_stays_fail_closed(self) -> None:
        nonce = "abcdefghijklmnopqrstuv"
        base: dict[str, object] = {
            "session_id": "session-a",
            "revision": 2,
            "transfer_nonce": nonce,
            "transfer_id": "r2-" + hashlib.sha256(nonce.encode()).hexdigest()[:16],
            "goal_identity": "goal:sha256:" + ("a" * 64),
            "objective": "Ship the handoff",
            "active_task": "Implement structural readiness",
            "phase": "verification",
            "status": "in progress",
            "completion_criteria": ["All tests pass"],
            "completed_work": [],
            "remaining_work": ["Implement runtime"],
            "constraints": ["No dependencies"],
            "decisions": [],
            "blockers": [],
            "authoritative_files": ["write_handoff.py::validate_structural_readiness"],
            "validation_evidence": [],
            "resume_validation": {
                "command": "python3 focused_test.py",
                "expected": "exit 0 and 7 tests pass",
            },
            "next_action": "Implement the exact resume contract",
        }
        critical = (
            "session_id",
            "revision",
            "transfer_nonce",
            "transfer_id",
            "goal_identity",
            "objective",
            "active_task",
            "phase",
            "status",
            "completion_criteria",
            "remaining_work",
            "constraints",
            "authoritative_files",
            "next_action",
        )
        self.assertTrue(validate_structural_readiness(base, session_id="session-a")["resume_ready"])
        for field in critical:
            with self.subTest(field=field, case="missing"):
                state = dict(base)
                del state[field]
                outcome = validate_structural_readiness(state, session_id="session-a")
                self.assertFalse(outcome["resume_ready"])
                self.assertIn(f"missing:{field}", outcome["failures"])
            with self.subTest(field=field, case="placeholder"):
                state = dict(base)
                state[field] = 0 if field == "revision" else (["TBD"] if isinstance(base[field], list) else "TBD")
                outcome = validate_structural_readiness(state, session_id="session-a")
                self.assertFalse(outcome["resume_ready"])
                expected = "missing:revision" if field == "revision" else f"placeholder:{field}"
                self.assertIn(expected, outcome["failures"])
        for key in ("command", "expected"):
            for case, value in (("missing", ""), ("placeholder", "TBD")):
                with self.subTest(field=f"resume_validation.{key}", case=case):
                    state = dict(base)
                    state["resume_validation"] = dict(base["resume_validation"], **{key: value})
                    outcome = validate_structural_readiness(state, session_id="session-a")
                    self.assertFalse(outcome["resume_ready"])
                    self.assertIn(f"{case}:resume_validation.{key}", outcome["failures"])

    def test_prompt_contract_binds_dynamic_values(self) -> None:
        prompt = build_continuation_prompt(
            Path("/tmp/handoff.md"),
            session_id="prompt-contract",
            revision=3,
            capsule_sha256="a" * 64,
            goal_identity="goal:sha256:" + ("b" * 64),
            transfer_nonce="abcdefghijklmnopqrstuv",
            transfer_id="r3-0123456789abcdef",
            next_action="Run the focused writer suite",
            resume_validation_command="python3 focused_test.py",
            resume_validation_expected="exit 0 and 7 tests pass",
        )
        self.assertIsInstance(prompt, str)
        assert isinstance(prompt, str)
        for value in (
            "/tmp/handoff.md",
            "prompt-contract",
            "3",
            "a" * 64,
            "goal:sha256:" + ("b" * 64),
            "abcdefghijklmnopqrstuv",
            "r3-0123456789abcdef",
            "Run the focused writer suite",
            "python3 focused_test.py",
            "exit 0 and 7 tests pass",
        ):
            self.assertIn(value, prompt)

    def test_validation_evidence_round_trips_independently_from_resume_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            args = rich_handoff_args(repo, session_id="validation-contract")
            validation_index = args.index("--validation-status")
            del args[validation_index : validation_index + 2]

            result = run_write_handoff(
                *args,
                "--validation-evidence",
                "Historical suite passed yesterday",
                "--validation-evidence",
                "Lint was green before the handoff",
                "--resume-validation-command",
                "python3 focused_test.py",
                "--resume-validation-expected",
                "exit 0 and 7 tests pass",
            )

            payload = json_payload(result)
            capsule = Path(str(payload["capsule_path"]))
            active = json.loads(
                (
                    repo
                    / ".omx"
                    / "state"
                    / "relay"
                    / "sessions"
                    / session_scope("validation-contract")
                    / ".active-task.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                active["validation_evidence"],
                ["Historical suite passed yesterday", "Lint was green before the handoff"],
            )
            self.assertEqual(
                active["resume_validation"],
                {"command": "python3 focused_test.py", "expected": "exit 0 and 7 tests pass"},
            )
            self.assertNotIn("validation", active)
            self.assertNotIn("next_step", active)
            content = capsule.read_text(encoding="utf-8")
            self.assertIn("Historical suite passed yesterday", content)
            self.assertIn("python3 focused_test.py", content)
            self.assertIn("exit 0 and 7 tests pass", content)

    def test_validation_evidence_prompt_binds_resume_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            args = rich_handoff_args(repo, session_id="validation-prompt")
            validation_index = args.index("--validation-status")
            del args[validation_index : validation_index + 2]

            payload = json_payload(
                run_write_handoff(
                    *args,
                    "--validation-evidence",
                    "Historical suite passed yesterday",
                    "--resume-validation-command",
                    "python3 focused_test.py",
                    "--resume-validation-expected",
                    "exit 0 and 7 tests pass",
                )
            )

            prompt = str(payload["continuation_prompt"])
            self.assertIn("python3 focused_test.py", prompt)
            self.assertIn("exit 0 and 7 tests pass", prompt)

    def test_validation_evidence_can_be_empty_when_resume_validation_is_complete(self) -> None:
        state: dict[str, object] = {
            "session_id": "session-a",
            "revision": 2,
            "transfer_nonce": "abcdefghijklmnopqrstuv",
            "transfer_id": "r2-" + hashlib.sha256(b"abcdefghijklmnopqrstuv").hexdigest()[:16],
            "goal_identity": "goal:sha256:" + ("a" * 64),
            "objective": "Ship the handoff",
            "active_task": "Implement structural readiness",
            "phase": "verification",
            "status": "in progress",
            "completion_criteria": ["All tests pass"],
            "completed_work": ["Added fixtures"],
            "remaining_work": ["Implement runtime"],
            "constraints": ["No dependencies"],
            "decisions": ["Use byte budgets"],
            "blockers": ["Host trust is unverified"],
            "authoritative_files": ["write_handoff.py::validate_structural_readiness"],
            "validation_evidence": [],
            "resume_validation": {
                "command": "python3 focused_test.py",
                "expected": "exit 0 and 7 tests pass",
            },
            "next_action": "Implement the exact resume contract",
        }

        ready = validate_structural_readiness(state, session_id="session-a")
        self.assertTrue(ready["resume_ready"])
        for missing in ("command", "expected"):
            with self.subTest(missing=missing):
                invalid = dict(state)
                invalid["resume_validation"] = {
                    "command": "" if missing == "command" else "python3 focused_test.py",
                    "expected": "" if missing == "expected" else "exit 0 and 7 tests pass",
                }
                outcome = validate_structural_readiness(invalid, session_id="session-a")
                self.assertFalse(outcome["resume_ready"])
                self.assertIn(f"missing:resume_validation.{missing}", outcome["failures"])

    def test_trim_lines_returns_text_unchanged_within_limit(self) -> None:
        text = "first\nsecond\n"
        self.assertEqual(trim_lines(text, 2), text)

    def test_trim_lines_reports_ascii_overflow(self) -> None:
        self.assertEqual(
            trim_lines("first\nsecond\nthird\nfourth", 2),
            "first\nsecond\n... (2 more lines omitted)",
        )

    def test_trim_lines_preserves_unicode_when_truncating(self) -> None:
        self.assertEqual(
            trim_lines("界 one\n🙂 two\n最後 three", 2),
            "界 one\n🙂 two\n... (1 more lines omitted)",
        )

    def test_threshold_blocks_below_30(self) -> None:
        result = run_write_handoff(
            "--objective",
            "test objective",
            "--next-step",
            "test next",
            "--context-used",
            "29%",
            "--handoff-threshold",
            "0.30",
            "--emit-json",
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["should_handoff"])
        self.assertTrue(payload["skipped"])
        self.assertIsNone(payload["capsule_path"])

    def test_threshold_writes_at_31(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            out = repo / "handoff.md"
            args = remove_arg(
                rich_handoff_args(repo, session_id="threshold-31"),
                "--force-handoff",
            )
            result = run_write_handoff(
                *args,
                "--out",
                str(out),
                "--context-used",
                "31%",
                "--handoff-threshold",
                "0.30",
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "should_handoff"))
            self.assertFalse(contract_value(payload, "skipped"))
            self.assertTrue(out.exists())
            content = out.read_text(encoding="utf-8")
            self.assertIn("Ship the token-efficient relay package", content)
            self.assertIn("Implement deterministic capsule budgeting", content)

    def test_threshold_writes_at_exact_30(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            out = repo / "handoff.md"
            args = remove_arg(
                rich_handoff_args(repo, session_id="threshold-exact"),
                "--force-handoff",
            )
            result = run_write_handoff(
                *args,
                "--out",
                str(out),
                "--context-used",
                "30%",
                "--handoff-threshold",
                "0.30",
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "should_handoff"))
            self.assertFalse(contract_value(payload, "skipped"))
            self.assertEqual(contract_value(payload, "context_used_ratio"), 0.30)
            self.assertTrue(out.exists())

    def test_force_handoff_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            out = repo / "forced.md"
            result = run_write_handoff(
                *rich_handoff_args(repo, session_id="forced-below"),
                "--out",
                str(out),
                "--context-used",
                "10%",
                "--handoff-threshold",
                "0.30",
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "should_handoff"))
            self.assertTrue(out.exists())

    def test_env_var_objective_and_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            out = repo / "env.md"
            args = remove_arg(
                rich_handoff_args(repo, session_id="env-state"),
                "--objective",
                takes_value=True,
            )
            args = remove_arg(args, "--next-step", takes_value=True)
            result = run_write_handoff(
                *args,
                "--out",
                str(out),
                env={
                    "RELAY_OBJECTIVE": "from env objective",
                    "RELAY_NEXT_STEP": "from env next",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            content = out.read_text(encoding="utf-8")
            self.assertIn("from env objective", content)
            self.assertIn("from env next", content)

    def test_extended_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            out = repo / "extended.md"
            result = run_write_handoff(
                *rich_handoff_args(repo, session_id="extended"),
                "--out",
                str(out),
                "--goal-objective",
                "ship the relay continuation",
                "--commands-run",
                "python3 -m py_compile scripts/write_handoff.py",
            )
            json_payload(result)
            content = out.read_text(encoding="utf-8")
            self.assertIn("Goal Mode Objective", content)
            self.assertIn("ship the relay continuation", content)
            self.assertIn("UTF-8 byte budgets are authoritative", content)
            self.assertIn("Live hook trust still requires host validation", content)
            self.assertIn("Regression suite added; runtime validation pending", content)

    def test_capsule_retains_resume_critical_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            out = repo / "retention.md"
            result = run_write_handoff(*rich_handoff_args(repo), "--out", str(out))
            payload = json_payload(result)
            content = out.read_text(encoding="utf-8")
            required = [
                "Ship the token-efficient relay package",
                "Lock the v2 capsule contract with deterministic tests",
                "regression suite",
                "implementation in progress",
                "Every required field round-trips and targeted tests pass",
                "Read the approved PRD and test specification",
                "Implement the canonical runtime and run validation",
                "Use only the Python standard library; preserve legacy bytes",
                "UTF-8 byte budgets are authoritative",
                "Live hook trust still requires host validation",
                "Regression suite added; runtime validation pending",
                "skills/relay/scripts/write_handoff.py::build_markdown",
                "Implement deterministic capsule budgeting in build_markdown",
            ]
            for value in required:
                with self.subTest(value=value):
                    self.assertIn(value, content)

            prompt = contract_value(payload, "continuation_prompt", "prompt")
            self.assertIsInstance(prompt, str)
            self.assertTrue(prompt)
            self.assertTrue(contract_value(payload, "resume_ready"))
            self.assertLessEqual(len(content.encode("utf-8")), 4096)
            self.assertLessEqual(len(str(prompt).encode("utf-8")), 1024)
            self.assertNotIn(str(prompt), content)

            prompt_occurrences = sum(
                value.count(str(prompt)) for value in all_string_values(payload)
            )
            self.assertEqual(prompt_occurrences, 1)

    def test_prompt_binds_capsule_and_transfer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            nonce = "abcdefghijklmnopqrstuv"
            result = run_write_handoff(
                *rich_handoff_args(repo, session_id="edge-structured"),
                "--revision",
                "7",
                "--transfer-nonce",
                nonce,
            )
            payload = json_payload(result)
            capsule = Path(str(payload["capsule_path"]))
            prompt = str(payload["continuation_prompt"])
            transfer_id = "r7-" + hashlib.sha256(nonce.encode()).hexdigest()[:16]
            digest = hashlib.sha256(capsule.read_bytes()).hexdigest()
            for value in (
                capsule,
                payload["session_id"],
                payload["revision"],
                digest,
                payload["goal_identity"],
                nonce,
                transfer_id,
                payload["next_action"],
                payload["resume_validation"]["command"],
                payload["resume_validation"]["expected"],
            ):
                self.assertIn(str(value), prompt)


class TokenEfficientCapsuleTests(unittest.TestCase):
    def test_ratio_capsule_and_prompt_defaults_are_independent(self) -> None:
        import context_handoff as context_module
        import write_handoff as write_module

        self.assertEqual(context_module.DEFAULT_THRESHOLD, 0.30)
        self.assertEqual(write_module.DEFAULT_CAPSULE_BUDGET_BYTES, 4096)
        self.assertEqual(write_module.DEFAULT_PROMPT_BUDGET_BYTES, 1024)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)

            capsule_override = run_write_handoff(
                *rich_handoff_args(repo, session_id="capsule-override"),
                "--capsule-budget-bytes",
                "3072",
            )
            capsule_payload = json_payload(capsule_override)
            self.assertEqual(contract_value(capsule_payload, "capsule_budget_bytes"), 3072)
            self.assertEqual(contract_value(capsule_payload, "prompt_budget_bytes"), 1024)
            self.assertEqual(contract_value(capsule_payload, "handoff_trigger_ratio", "handoff_threshold"), 0.30)

            prompt_override = run_write_handoff(
                *rich_handoff_args(repo, session_id="prompt-override"),
                "--prompt-budget-bytes",
                "768",
            )
            prompt_payload = json_payload(prompt_override)
            self.assertEqual(contract_value(prompt_payload, "capsule_budget_bytes"), 4096)
            self.assertEqual(contract_value(prompt_payload, "prompt_budget_bytes"), 768)
            self.assertEqual(contract_value(prompt_payload, "handoff_trigger_ratio", "handoff_threshold"), 0.30)

    def test_direct_writer_rejects_budget_overrides_above_canonical_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)

            for flag, value, expected_error in (
                (
                    "--capsule-budget-bytes",
                    "4097",
                    "capsule byte budget cannot exceed 4096 bytes",
                ),
                (
                    "--prompt-budget-bytes",
                    "1025",
                    "prompt byte budget cannot exceed 1024 bytes",
                ),
            ):
                with self.subTest(flag=flag):
                    out = repo / f"{flag.removeprefix('--')}.md"
                    result = run_write_handoff(
                        *rich_handoff_args(repo, session_id=f"oversized-{flag}"),
                        "--out",
                        str(out),
                        flag,
                        value,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr.strip(), expected_error)
                    self.assertFalse(out.exists())

    def test_orchestrator_rejects_oversized_budgets_without_state_or_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)

            for flag, value, expected_error in (
                (
                    "--capsule-budget-bytes",
                    "4097",
                    "capsule byte budget cannot exceed 4096 bytes",
                ),
                (
                    "--prompt-budget-bytes",
                    "1025",
                    "prompt byte budget cannot exceed 1024 bytes",
                ),
            ):
                with self.subTest(flag=flag):
                    result = run_context_handoff(
                        *rich_context_args(repo),
                        "--session-id",
                        f"oversized-{flag}",
                        "--trigger",
                        "manual",
                        flag,
                        value,
                    )
                    self.assertEqual(result.returncode, 2)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["error"], expected_error)
                    self.assertFalse(payload["should_handoff"])
                    self.assertFalse(payload["checkpoint_written"])
                    self.assertFalse(payload["delivery_emitted"])

            self.assertFalse(
                (repo / ".omx/state/relay").exists()
            )

    def test_official_hook_oversized_budget_fails_open_without_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            result = run_context_handoff(
                *rich_context_args(repo),
                "--session-id",
                "oversized-hook",
                "--trigger",
                "manual",
                "--capsule-budget-bytes",
                "4097",
                "--official-hook-event",
                "UserPromptSubmit",
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {"continue": True})
            self.assertFalse(
                (repo / ".omx/state/relay").exists()
            )

    def test_utf8_byte_budget_is_authoritative_and_token_count_is_approximate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            args = [
                *rich_handoff_args(repo, session_id="utf8-budget"),
                "--note",
                "Validated multibyte pruning: " + ("界" * 1800),
            ]
            result = run_write_handoff(*args)
            payload = json_payload(result)
            capsule = Path(str(contract_value(payload, "capsule_path")))
            capsule_bytes = len(capsule.read_bytes())
            prompt = contract_value(payload, "continuation_prompt", "prompt")

            self.assertLessEqual(capsule_bytes, 4096)
            self.assertEqual(contract_value(payload, "capsule_bytes"), capsule_bytes)
            self.assertIsInstance(prompt, str)
            self.assertLessEqual(len(str(prompt).encode("utf-8")), 1024)
            self.assertEqual(
                contract_value(payload, "prompt_bytes"),
                len(str(prompt).encode("utf-8")),
            )
            token_label = contract_value(
                payload,
                "token_estimate_label",
                "token_count_label",
                "approximate_token_label",
            )
            self.assertIsInstance(token_label, str)
            self.assertIn("approx", str(token_label).lower())
            capsule.read_text(encoding="utf-8")

    def test_critical_overflow_writes_verified_artifact_and_blocks_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            huge = "critical-界-" + ("界" * 2200)
            args = replace_arg(
                rich_handoff_args(repo, session_id="critical-overflow"),
                "--objective",
                huge,
            )
            result = run_write_handoff(*args)
            payload = json_payload(result)

            self.assertIs(contract_value(payload, "resume_ready"), False)
            self.assertIs(contract_value(payload, "should_handoff"), False)
            self.assertIsNone(contract_value(payload, "continuation_prompt", "prompt"))
            self.assertIs(
                contract_value(payload, "delivery_emitted", "delivered"),
                False,
            )

            overflow = contract_value(payload, "overflow")
            self.assertIsInstance(overflow, dict)
            assert isinstance(overflow, dict)
            self.assertEqual(set(overflow), {"path", "sha256"})
            overflow_path = Path(str(overflow["path"]))
            self.assertTrue(overflow_path.is_file())
            overflow_bytes = overflow_path.read_bytes()
            self.assertEqual(hashlib.sha256(overflow_bytes).hexdigest(), overflow["sha256"])
            self.assertIn(str(overflow["sha256"]), overflow_path.name)

            capsule = Path(str(contract_value(payload, "capsule_path")))
            capsule_text = capsule.read_text(encoding="utf-8")
            self.assertLessEqual(len(capsule_text.encode("utf-8")), 4096)
            self.assertIn("resume_ready", capsule_text)
            self.assertIn("false", capsule_text.lower())
            self.assertIn(str(overflow["path"]), capsule_text)
            self.assertIn(str(overflow["sha256"]), capsule_text)

    def test_optional_overflow_reference_that_cannot_fit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            args = replace_arg(
                rich_handoff_args(repo, session_id="optional-reference-overflow"),
                "--objective",
                "x" * 2000,
            )
            result = run_write_handoff(
                *args,
                "--note",
                "optional evidence " + ("y" * 500),
            )
            payload = json_payload(result)

            self.assertIs(contract_value(payload, "resume_ready"), False)
            self.assertIs(contract_value(payload, "should_handoff"), False)
            self.assertIsNone(contract_value(payload, "continuation_prompt", "prompt"))
            self.assertIs(contract_value(payload, "delivery_emitted", "delivered"), False)
            overflow = contract_value(payload, "overflow")
            self.assertIsInstance(overflow, dict)
            assert isinstance(overflow, dict)
            overflow_path = Path(str(overflow["path"]))
            self.assertEqual(
                hashlib.sha256(overflow_path.read_bytes()).hexdigest(),
                overflow["sha256"],
            )
            capsule_path = Path(str(contract_value(payload, "capsule_path")))
            capsule_text = capsule_path.read_text(encoding="utf-8")
            self.assertLessEqual(len(capsule_text.encode("utf-8")), 4096)
            self.assertIn("critical:overflow_reference_budget_exceeded", capsule_text)
            self.assertIn(str(overflow["sha256"]), capsule_text)

    def test_structural_guard_rejects_only_declared_structural_failures(self) -> None:
        import context_handoff as context_module

        guard = context_module.validate_structural_readiness
        base: dict[str, object] = {
            "session_id": "session-a",
            "revision": 2,
            "transfer_nonce": "abcdefghijklmnopqrstuv",
            "transfer_id": "r2-" + hashlib.sha256(b"abcdefghijklmnopqrstuv").hexdigest()[:16],
            "goal_identity": "goal:sha256:" + ("a" * 64),
            "objective": "Ship the handoff",
            "active_task": "Implement structural readiness",
            "phase": "verification",
            "status": "in progress",
            "completion_criteria": ["All tests pass"],
            "completed_work": ["Added fixtures"],
            "remaining_work": ["Implement runtime"],
            "constraints": ["No dependencies"],
            "decisions": ["Use byte budgets"],
            "blockers": ["Host trust is unverified"],
            "authoritative_files": ["skills/relay/scripts/context_handoff.py::main"],
            "validation_evidence": ["Tests collected"],
            "resume_validation": {
                "command": "python3 focused_test.py",
                "expected": "exit 0 and 7 tests pass",
            },
            "next_action": "Implement validate_structural_readiness",
        }

        valid = guard(base, session_id="session-a")
        self.assertTrue(valid["resume_ready"])
        self.assertEqual(valid["failures"], [])

        cases: list[tuple[str, dict[str, object], str, str]] = []
        missing = dict(base)
        del missing["completion_criteria"]
        cases.append(("missing", missing, "session-a", "missing"))
        placeholder = dict(base, objective="TBD")
        cases.append(("placeholder", placeholder, "session-a", "placeholder"))
        circular = dict(
            base,
            next_action="Read this capsule and resume from the recorded Next Action",
        )
        cases.append(("circular", circular, "session-a", "circular"))
        cross_session = dict(base)
        cases.append(("cross-session", cross_session, "session-b", "session"))
        overlap = dict(base, remaining_work=["Added fixtures"])
        cases.append(("overlap", overlap, "session-a", "overlap"))

        for label, state, session_id, reason_fragment in cases:
            with self.subTest(label=label):
                outcome = guard(
                    state,
                    session_id=session_id,
                )
                self.assertFalse(outcome["resume_ready"])
                failures = " ".join(str(value) for value in outcome["failures"])
                self.assertIn(reason_fragment, failures.lower())
                self.assertNotIn("semantic contradiction", failures.lower())
                self.assertNotIn("boilerplate", failures.lower())

    def test_writer_revision_is_structural_and_bool_safe(self) -> None:
        import context_handoff as context_module

        guard = context_module.validate_structural_readiness
        base: dict[str, object] = {
            "session_id": "session-a",
            "revision": 1,
            "transfer_nonce": "abcdefghijklmnopqrstuv",
            "transfer_id": "r1-" + hashlib.sha256(b"abcdefghijklmnopqrstuv").hexdigest()[:16],
            "goal_identity": "goal:sha256:" + ("a" * 64),
            "objective": "Ship the handoff",
            "active_task": "Implement structural readiness",
            "phase": "verification",
            "status": "in progress",
            "completion_criteria": ["All tests pass"],
            "completed_work": ["Added fixtures"],
            "remaining_work": ["Implement runtime"],
            "constraints": ["No dependencies"],
            "decisions": ["Use byte budgets"],
            "blockers": ["Host trust is unverified"],
            "authoritative_files": ["skills/relay/scripts/context_handoff.py::main"],
            "validation_evidence": ["Tests collected"],
            "resume_validation": {
                "command": "python3 focused_test.py",
                "expected": "exit 0 and 7 tests pass",
            },
            "next_action": "Implement validate_structural_readiness",
        }

        self.assertTrue(guard(base, session_id="session-a")["resume_ready"])
        for revision in (True, False, 0, -1):
            with self.subTest(revision=revision):
                outcome = guard({**base, "revision": revision}, session_id="session-a")
                self.assertFalse(outcome["resume_ready"])
                self.assertIn("missing:revision", outcome["failures"])

class ContextHandoffTests(unittest.TestCase):
    def test_timing_strategy_matrix_is_deterministic_and_budget_independent(self) -> None:
        import context_handoff as context_module

        for trigger in ("manual", "pre-compact"):
            with self.subTest(strategy=trigger, telemetry="missing"):
                self.assertTrue(
                    context_module.should_trigger_handoff(
                        trigger=trigger,
                        context_ratio=None,
                        threshold=0.30,
                        force=False,
                    )
                )

        invalid_telemetry: tuple[dict[str, object], ...] = (
            {},
            {"contextUsed": "unknown"},
            {"contextUsed": "nan"},
            {"context_usage_percent": 101},
            {"context_tokens": 31, "context_window_size": 0},
        )
        for payload in invalid_telemetry:
            with self.subTest(strategy="threshold", telemetry=payload):
                ratio = context_module.extract_context_used(payload)
                self.assertIsNone(ratio)
                self.assertFalse(
                    context_module.should_trigger_handoff(
                        trigger="threshold",
                        context_ratio=ratio,
                        threshold=0.30,
                        force=False,
                    )
                )

        for threshold in (0.30, 0.50, 0.70):
            for label, ratio, expected in (
                ("threshold-epsilon", threshold - 0.001, False),
                ("exact-threshold", threshold, True),
                ("threshold-plus-epsilon", threshold + 0.001, True),
            ):
                with self.subTest(threshold=threshold, boundary=label):
                    self.assertIs(
                        context_module.should_trigger_handoff(
                            trigger="threshold",
                            context_ratio=ratio,
                            threshold=threshold,
                            force=False,
                        ),
                        expected,
                    )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            for index, (capsule_budget, prompt_budget) in enumerate(
                ((4096, 1024), (4095, 1023))
            ):
                with self.subTest(
                    capsule_budget=capsule_budget,
                    prompt_budget=prompt_budget,
                ):
                    args = replace_arg(
                        rich_context_args(repo),
                        "--session-id",
                        f"timing-budget-{index}",
                    )
                    result = run_context_handoff(
                        *args,
                        "--stdin-json",
                        "--dedup-seconds",
                        "0",
                        "--handoff-threshold",
                        "0.50",
                        "--capsule-budget-bytes",
                        str(capsule_budget),
                        "--prompt-budget-bytes",
                        str(prompt_budget),
                        stdin=json.dumps(
                            {
                                "session_id": f"timing-budget-{index}",
                                "prompt": "continue",
                                "contextUsed": 0.50,
                            }
                        ),
                    )
                    payload = json_payload(result)
                    self.assertEqual(payload["context_used_ratio"], 0.50)
                    self.assertIs(payload["should_handoff"], True)

    def test_validation_evidence_legacy_read_does_not_synthesize_resume_validation(self) -> None:
        import context_handoff as context_module

        args = argparse.Namespace(
            session_id="legacy-session",
            objective="",
            active_task="",
            phase="",
            status="",
            completion_criteria=[],
            completed_work=[],
            remaining_work=[],
            constraints=[],
            decisions=[],
            blockers=[],
            authoritative_files=[],
            validation_evidence=[],
            resume_validation_command="",
            resume_validation_expected="",
            next_step="",
            goal_objective="",
            goal_identity="goal-id",
        )
        state = context_module.merge_state(
            args,
            {
                "validation": ["Historical legacy validation"],
                "next_step": "Continue the legacy action",
            },
        )

        self.assertEqual(state["validation_evidence"], ["Historical legacy validation"])
        self.assertEqual(state["next_action"], "Continue the legacy action")
        self.assertEqual(state["resume_validation"], {"command": "", "expected": ""})

    def test_authoritative_revision_stale_current_new(self) -> None:
        import context_handoff as context_module

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_id = "authoritative-revision"
            args = replace_arg(rich_context_args(repo), "--session-id", session_id)
            json_payload(run_context_handoff(*args, "--trigger", "pre-compact"))
            current_payload = json_payload(
                run_context_handoff(*args, "--trigger", "pre-compact")
            )
            paths = context_module.state_paths(repo, session_id)
            durable = json.loads(paths.revision.read_text(encoding="utf-8"))
            self.assertEqual(durable["revision"], 2)

            stale_intent = {
                "version": 1,
                "session_id": session_id,
                "source_session_id": session_id,
                "state_sha256": durable["state_sha256"],
                "goal_identity": durable["goal_identity"],
                "revision": 1,
                "capsule_path": str(paths.session_dir / "stale-r1-handoff.md"),
                "transfer_nonce": "stale-revision-nonce-1234",
                "writer_complete": False,
            }
            paths.prepare_intent.write_text(
                json.dumps(stale_intent, sort_keys=True),
                encoding="utf-8",
            )
            before = {
                path: path.read_bytes()
                for path in (
                    paths.revision,
                    paths.session_pointer,
                    paths.latest_pointer,
                    paths.prepare_intent,
                )
            }

            stale = run_context_handoff(*args, "--trigger", "pre-compact")

            self.assertNotEqual(stale.returncode, 0)
            stale_error = json.loads(stale.stdout)
            self.assertEqual(
                stale_error["error"],
                "prepare intent conflicts with current handoff state",
            )
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in before},
            )

            current_intent = {
                "version": 1,
                "session_id": session_id,
                "source_session_id": session_id,
                "state_sha256": durable["state_sha256"],
                "goal_identity": durable["goal_identity"],
                "revision": 2,
                "capsule_path": current_payload["capsule_path"],
                "transfer_nonce": current_payload["transfer_nonce"],
                "writer_complete": True,
                "write_result": current_payload,
            }
            paths.prepare_intent.write_text(
                json.dumps(current_intent, sort_keys=True),
                encoding="utf-8",
            )
            retried = json_payload(
                run_context_handoff(*args, "--trigger", "pre-compact")
            )
            self.assertEqual(retried["revision"], 2)
            self.assertEqual(retried["capsule_path"], current_payload["capsule_path"])
            self.assertEqual(retried["transfer_nonce"], current_payload["transfer_nonce"])
            self.assertFalse(paths.prepare_intent.exists())

            allocated = json_payload(
                run_context_handoff(*args, "--trigger", "pre-compact")
            )
            self.assertEqual(allocated["revision"], 3)
            self.assertEqual(
                json.loads(paths.revision.read_text(encoding="utf-8"))["revision"],
                3,
            )

    def test_authoritative_revision_rejects_bool_state_and_intent(self) -> None:
        import context_handoff as context_module

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_id = "bool-revision"
            args = replace_arg(rich_context_args(repo), "--session-id", session_id)
            first = json_payload(
                run_context_handoff(*args, "--trigger", "pre-compact")
            )
            paths = context_module.state_paths(repo, session_id)
            durable = json.loads(paths.revision.read_text(encoding="utf-8"))
            bool_intent = {
                "version": 1,
                "session_id": session_id,
                "source_session_id": session_id,
                "state_sha256": durable["state_sha256"],
                "goal_identity": durable["goal_identity"],
                "revision": True,
                "capsule_path": first["capsule_path"],
                "transfer_nonce": first["transfer_nonce"],
                "writer_complete": True,
                "write_result": first,
            }
            paths.prepare_intent.write_text(
                json.dumps(bool_intent, sort_keys=True),
                encoding="utf-8",
            )
            revision_before = paths.revision.read_bytes()

            rejected = run_context_handoff(*args, "--trigger", "pre-compact")

            self.assertNotEqual(rejected.returncode, 0)
            rejected_error = json.loads(rejected.stdout)
            self.assertEqual(
                rejected_error["error"],
                "prepare intent conflicts with current handoff state",
            )
            self.assertEqual(paths.revision.read_bytes(), revision_before)

            invalid_revisions = (
                ("bool", True),
                ("malformed", "malformed"),
                ("negative", -1),
            )
            for label, invalid_revision in invalid_revisions:
                with self.subTest(durable_revision=label):
                    durable_session_id = f"{label}-durable-revision"
                    durable_args = replace_arg(
                        rich_context_args(repo),
                        "--session-id",
                        durable_session_id,
                    )
                    first = json_payload(
                        run_context_handoff(
                            *durable_args,
                            "--trigger",
                            "pre-compact",
                        )
                    )
                    durable_paths = context_module.state_paths(
                        repo,
                        durable_session_id,
                    )
                    durable_state = json.loads(
                        durable_paths.revision.read_text(encoding="utf-8")
                    )
                    retry_intent = {
                        "version": 1,
                        "session_id": durable_session_id,
                        "source_session_id": durable_session_id,
                        "state_sha256": durable_state["state_sha256"],
                        "goal_identity": durable_state["goal_identity"],
                        "revision": 1,
                        "capsule_path": first["capsule_path"],
                        "transfer_nonce": first["transfer_nonce"],
                        "writer_complete": True,
                        "write_result": first,
                    }
                    durable_paths.prepare_intent.write_text(
                        json.dumps(retry_intent, sort_keys=True),
                        encoding="utf-8",
                    )
                    durable_paths.revision.write_text(
                        json.dumps(
                            {**durable_state, "revision": invalid_revision},
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    before = {
                        path: path.read_bytes()
                        for path in (
                            durable_paths.revision,
                            durable_paths.session_pointer,
                            durable_paths.latest_pointer,
                            durable_paths.prepare_intent,
                        )
                    }
                    capsule_root = repo / ".omx/state/relay"
                    capsules_before = {
                        path: path.read_bytes()
                        for path in capsule_root.rglob("*-handoff.md")
                    }

                    rejected = run_context_handoff(
                        *durable_args,
                        "--trigger",
                        "pre-compact",
                    )

                    self.assertNotEqual(rejected.returncode, 0)
                    rejected_error = json.loads(rejected.stdout)
                    self.assertEqual(
                        rejected_error["error"],
                        "durable revision is invalid",
                    )
                    self.assertEqual(
                        before,
                        {path: path.read_bytes() for path in before},
                    )
                    self.assertEqual(
                        capsules_before,
                        {
                            path: path.read_bytes()
                            for path in capsule_root.rglob("*-handoff.md")
                        },
                    )

    def test_concurrent_revision_allocation_is_contiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_id = "concurrent-revisions"
            args = replace_arg(rich_context_args(repo), "--session-id", session_id)

            def allocate(_index: int) -> dict[str, object]:
                return json_payload(
                    run_context_handoff(*args, "--trigger", "pre-compact")
                )

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(allocate, range(6)))

            revisions = sorted(item["revision"] for item in results)
            self.assertEqual(revisions, list(range(1, 7)))
            self.assertTrue(all(type(revision) is int for revision in revisions))
            self.assertEqual(len(set(revisions)), len(revisions))
            _pointer_path, pointer = find_latest_pointer(repo)
            self.assertEqual(pointer["revision"], 6)
            capsules = list(
                (repo / ".omx/state/relay").rglob("*-handoff.md")
            )
            self.assertEqual(len(capsules), 6)

    def test_context_handoff_blocks_below_30(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=False)
            result = run_context_handoff(
                "--repo",
                str(repo),
                "--stdin-json",
                "--objective",
                "below threshold",
                "--next-step",
                "continue",
                stdin=json.dumps({"contextUsed": "0.29"}),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["should_handoff"])
            self.assertIsNone(payload["capsule_path"])

    def test_context_handoff_writes_at_exact_30(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            result = run_context_handoff(
                *rich_context_args(repo),
                "--stdin-json",
                "--dedup-seconds",
                "0",
                stdin=json.dumps(
                    {
                        "session_id": "session-rich",
                        "prompt": "continue",
                        "context_usage_percent": 30,
                    }
                ),
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "should_handoff"))
            self.assertEqual(contract_value(payload, "context_used_ratio"), 0.30)
            self.assertIsNotNone(contract_value(payload, "capsule_path"))

    def test_parses_stdin_context_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            result = run_context_handoff(
                *rich_context_args(repo),
                "--stdin-json",
                "--dedup-seconds",
                "0",
                stdin=json.dumps(
                    {
                        "session_id": "session-rich",
                        "prompt": "continue",
                        "contextUsed": "0.31",
                    }
                ),
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "should_handoff"))
            self.assertIsNotNone(contract_value(payload, "capsule_path"))

    def test_parses_nested_and_token_pair_context_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            nested = run_context_handoff(
                *rich_context_args(repo),
                "--stdin-json",
                "--dedup-seconds",
                "0",
                stdin=json.dumps(
                    {
                        "session_id": "session-rich",
                        "prompt": "continue",
                        "telemetry": {"usagePercent": "31%"},
                    }
                ),
            )
            nested_payload = json_payload(nested)
            self.assertTrue(contract_value(nested_payload, "should_handoff"))
            self.assertEqual(contract_value(nested_payload, "context_used_ratio"), 0.31)

            token_pair = run_context_handoff(
                *replace_arg(rich_context_args(repo), "--session-id", "token-pair"),
                "--stdin-json",
                "--dedup-seconds",
                "0",
                stdin=json.dumps(
                    {
                        "session_id": "token-pair",
                        "prompt": "continue",
                        "context_tokens": 31,
                        "context_window_size": 100,
                    }
                ),
            )
            token_payload = json_payload(token_pair)
            self.assertTrue(contract_value(token_payload, "should_handoff"))
            self.assertEqual(contract_value(token_payload, "context_used_ratio"), 0.31)

    def test_official_percent_field_treats_one_as_one_percent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=False)
            result = run_context_handoff(
                "--repo",
                str(repo),
                "--stdin-json",
                "--objective",
                "percent semantics",
                "--next-step",
                "continue",
                stdin=json.dumps({"context_usage_percent": 1}),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["context_used_ratio"], 0.01)
            self.assertFalse(payload["should_handoff"])

    def test_invalid_stdin_json_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=False)
            result = run_context_handoff(
                "--repo",
                str(repo),
                "--stdin-json",
                stdin="{not-json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(result.stdout)["should_handoff"])

    def test_invalid_compatibility_telemetry_is_safe_and_never_triggers(self) -> None:
        invalid_payloads: list[dict[str, object]] = [
            {"contextUsed": -0.01},
            {"contextUsed": "nan"},
            {"contextUsed": "inf"},
            {"contextUsed": {"forged": 0.99}},
            {"context_usage_percent": 101},
            {"context_usage_percent": -1},
            {"context_tokens": -1, "context_window_size": 100},
            {"context_tokens": 31, "context_window_size": 0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            for index, telemetry in enumerate(invalid_payloads):
                with self.subTest(telemetry=telemetry):
                    hook_payload = {
                        "session_id": f"invalid-{index}",
                        "prompt": "continue safely",
                        **telemetry,
                    }
                    result = run_context_handoff(
                        "--repo",
                        str(repo),
                        "--stdin-json",
                        stdin=json.dumps(hook_payload),
                    )
                    payload = json_payload(result)
                    self.assertIsNone(contract_value(payload, "context_used_ratio"))
                    self.assertFalse(contract_value(payload, "should_handoff"))
                    self.assertIsNone(contract_value(payload, "capsule_path"))

    def test_rejects_threshold_outside_ratio_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_context_handoff(
                "--repo",
                tmp,
                "--handoff-threshold",
                "30",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("between 0 and 1", json.loads(result.stdout)["error"])

    def test_pre_compact_force_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            result = run_context_handoff(
                *rich_context_args(repo),
                "--trigger",
                "pre-compact",
                "--dedup-seconds",
                "0",
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "checkpoint_written", "revision_created"))
            self.assertTrue(contract_value(payload, "resume_ready"))
            self.assertTrue(contract_value(payload, "delivery_emitted", "delivered"))
            self.assertEqual(contract_value(payload, "handoff_mode"), "clean_task")
            prompt = contract_value(payload, "continuation_prompt", "prompt")
            self.assertIsInstance(prompt, str)
            self.assertEqual(
                sum(value.count(str(prompt)) for value in all_string_values(payload)),
                1,
            )

    def test_threshold_handoff_uses_clean_task_and_pointer_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            result = run_context_handoff(
                *rich_context_args(repo),
                "--stdin-json",
                "--dedup-seconds",
                "0",
                stdin=json.dumps(
                    {
                        "session_id": "session-rich",
                        "prompt": "continue the current task",
                        "contextUsed": "0.31",
                    }
                ),
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "should_handoff"))
            self.assertEqual(contract_value(payload, "handoff_mode"), "clean_task")
            prompt = contract_value(payload, "continuation_prompt", "prompt")
            self.assertIsInstance(prompt, str)

            _pointer_path, record = find_latest_pointer(repo)
            pointer_text = json.dumps(record, sort_keys=True)
            keys = {str(key).lower() for key in record}
            self.assertTrue(any("capsule" in key and "path" in key for key in keys))
            self.assertTrue(any("sha" in key or "hash" in key for key in keys))
            self.assertTrue(any("session" in key for key in keys))
            self.assertTrue(any("revision" in key for key in keys))
            self.assertTrue(any("deliver" in key for key in keys))
            self.assertTrue(any("metric" in key for key in keys))
            for forbidden in (
                "continuation_prompt",
                "prompt",
                "capsule_body",
                "objective",
                "active_task",
            ):
                self.assertNotIn(forbidden, keys)
            self.assertEqual(
                record["next_action"],
                "Implement deterministic capsule budgeting in build_markdown",
            )
            self.assertEqual(
                record["resume_validation"],
                {
                    "command": "python3 focused_test.py",
                    "expected": "exit 0 and 7 tests pass",
                },
            )
            self.assertNotIn(str(prompt), pointer_text)

    def test_dedup_reuses_recent_handoff_but_allows_later_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            hook_payload = {
                "session_id": "dedup-session",
                "prompt": "continue",
                "contextUsed": "0.31",
            }
            args = replace_arg(
                rich_context_args(repo),
                "--session-id",
                "dedup-session",
            )
            first = run_context_handoff(
                *args,
                "--stdin-json",
                stdin=json.dumps(hook_payload),
            )
            first_payload = json_payload(first)
            self.assertTrue(contract_value(first_payload, "should_handoff"))
            self.assertFalse(contract_value(first_payload, "deduped"))

            second = run_context_handoff(
                *args,
                "--stdin-json",
                stdin=json.dumps(hook_payload),
            )
            second_payload = json_payload(second)
            self.assertTrue(contract_value(second_payload, "deduped"))
            self.assertFalse(contract_value(second_payload, "should_handoff"))
            self.assertIsNone(contract_value(second_payload, "continuation_prompt", "prompt"))
            self.assertEqual(
                contract_value(first_payload, "capsule_path"),
                contract_value(second_payload, "capsule_path"),
            )

            third = run_context_handoff(
                *args,
                "--stdin-json",
                "--dedup-seconds",
                "0",
                stdin=json.dumps(hook_payload),
            )
            third_payload = json_payload(third)
            self.assertTrue(contract_value(third_payload, "should_handoff"))
            self.assertFalse(contract_value(third_payload, "deduped"))
            self.assertNotEqual(
                contract_value(first_payload, "capsule_path"),
                contract_value(third_payload, "capsule_path"),
            )

    def test_precompact_refreshes_revision_inside_delivery_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_id = "refresh-inside-cooldown"
            first_args = replace_arg(
                rich_context_args(repo),
                "--session-id",
                session_id,
            )
            first = run_context_handoff(
                *first_args,
                "--stdin-json",
                stdin=json.dumps(
                    {
                        "session_id": session_id,
                        "prompt": "continue",
                        "contextUsed": 0.31,
                    }
                ),
            )
            first_payload = json_payload(first)
            self.assertTrue(contract_value(first_payload, "delivery_emitted", "delivered"))
            first_revision = contract_value(first_payload, "revision")
            first_capsule = contract_value(first_payload, "capsule_path")

            refreshed_args = replace_arg(
                first_args,
                "--status",
                "PreCompact arrived after validation changed the durable state",
            )
            second = run_context_handoff(
                *refreshed_args,
                "--trigger",
                "pre-compact",
            )
            second_payload = json_payload(second)
            second_revision = contract_value(second_payload, "revision")
            second_capsule = contract_value(second_payload, "capsule_path")

            self.assertTrue(contract_value(second_payload, "checkpoint_written", "revision_created"))
            self.assertTrue(contract_value(second_payload, "resume_ready"))
            self.assertIsInstance(first_revision, int)
            self.assertIsInstance(second_revision, int)
            assert isinstance(first_revision, int)
            assert isinstance(second_revision, int)
            self.assertGreater(second_revision, first_revision)
            self.assertNotEqual(second_capsule, first_capsule)
            self.assertFalse(contract_value(second_payload, "delivery_emitted", "delivered"))
            self.assertTrue(contract_value(second_payload, "deduped"))
            self.assertIsNone(
                contract_value(second_payload, "continuation_prompt", "prompt")
            )
            capsules = list(
                (repo / ".omx/state/relay").rglob("*-handoff.md")
            )
            self.assertEqual(len(capsules), 2)

            _pointer_path, latest = find_latest_pointer(repo)
            self.assertEqual(contract_value(latest, "revision"), second_revision)
            self.assertEqual(contract_value(latest, "capsule_path"), second_capsule)

    def test_unchanged_precompact_always_refreshes_revision_but_dedupes_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_id = "unchanged-precompact-refresh"
            args = replace_arg(rich_context_args(repo), "--session-id", session_id)
            first_payload = json_payload(
                run_context_handoff(
                    *args,
                    "--stdin-json",
                    stdin=json.dumps(
                        {
                            "session_id": session_id,
                            "prompt": "continue",
                            "contextUsed": 0.31,
                        }
                    ),
                )
            )
            second_payload = json_payload(
                run_context_handoff(*args, "--trigger", "pre-compact")
            )
            first_revision = contract_value(first_payload, "revision")
            second_revision = contract_value(second_payload, "revision")
            self.assertIsInstance(first_revision, int)
            self.assertIsInstance(second_revision, int)
            assert isinstance(first_revision, int)
            assert isinstance(second_revision, int)

            self.assertGreater(
                second_revision,
                first_revision,
            )
            self.assertNotEqual(
                contract_value(second_payload, "capsule_path"),
                contract_value(first_payload, "capsule_path"),
            )
            self.assertTrue(contract_value(second_payload, "checkpoint_written"))
            self.assertTrue(contract_value(second_payload, "deduped"))
            self.assertFalse(contract_value(second_payload, "delivery_emitted"))
            self.assertIsNone(contract_value(second_payload, "continuation_prompt"))

    def test_corrupt_reused_pointer_creates_fresh_verified_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_id = "corrupt-pointer-refresh"
            args = replace_arg(rich_context_args(repo), "--session-id", session_id)
            hook_payload = {
                "session_id": session_id,
                "prompt": "continue",
                "contextUsed": 0.31,
            }
            first_payload = json_payload(
                run_context_handoff(
                    *args,
                    "--stdin-json",
                    stdin=json.dumps(hook_payload),
                )
            )
            first_path = Path(str(contract_value(first_payload, "capsule_path")))
            first_path.write_text(
                first_path.read_text(encoding="utf-8") + "\ntampered\n",
                encoding="utf-8",
            )

            second_payload = json_payload(
                run_context_handoff(
                    *args,
                    "--stdin-json",
                    stdin=json.dumps(hook_payload),
                )
            )
            second_path = Path(str(contract_value(second_payload, "capsule_path")))
            first_revision = contract_value(first_payload, "revision")
            second_revision = contract_value(second_payload, "revision")
            self.assertIsInstance(first_revision, int)
            self.assertIsInstance(second_revision, int)
            assert isinstance(first_revision, int)
            assert isinstance(second_revision, int)
            self.assertTrue(contract_value(second_payload, "checkpoint_written"))
            self.assertGreater(
                second_revision,
                first_revision,
            )
            self.assertNotEqual(second_path, first_path)
            self.assertEqual(
                hashlib.sha256(second_path.read_bytes()).hexdigest(),
                contract_value(second_payload, "capsule_sha256"),
            )
            self.assertFalse(contract_value(second_payload, "delivery_emitted"))

    def test_reused_pointer_requires_matching_identity_containment_and_readiness(self) -> None:
        import context_handoff as context_module

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_id = "pointer-validation"
            paths = context_module.state_paths(repo, session_id)
            paths.session_dir.mkdir(parents=True)
            capsule = paths.session_dir / "r3-handoff.md"
            capsule.write_text("ready", encoding="utf-8")
            digest = hashlib.sha256(capsule.read_bytes()).hexdigest()
            valid: dict[str, object] = {
                "capsule_path": str(capsule),
                "capsule_sha256": digest,
                "session_id": session_id,
                "session_scope": context_module.session_scope(session_id),
                "revision": 3,
                "resume_ready": True,
                "goal_identity": "goal:sha256:" + "1" * 64,
                "transfer_nonce": "abcdefghijklmnopqrstuv",
                "transfer_id": "r3-0123456789abcdef",
            }
            self.assertEqual(
                context_module.validate_reusable_pointer(
                    valid,
                    paths=paths,
                    session_id=session_id,
                    revision=3,
                ),
                [],
            )
            outside = repo / "outside.md"
            outside.write_text("ready", encoding="utf-8")
            outside_digest = hashlib.sha256(outside.read_bytes()).hexdigest()
            mutations: dict[str, dict[str, object]] = {
                "session": {"session_id": "other"},
                "scope": {"session_scope": "wrong"},
                "revision": {"revision": 2},
                "containment": {
                    "capsule_path": str(outside),
                    "capsule_sha256": outside_digest,
                },
                "hash": {"capsule_sha256": "0" * 64},
                "file": {"capsule_path": str(paths.session_dir / "missing.md")},
                "readiness": {"resume_ready": False},
            }
            for name, mutation in mutations.items():
                with self.subTest(name=name):
                    candidate = {**valid, **mutation}
                    self.assertTrue(
                        context_module.validate_reusable_pointer(
                            candidate,
                            paths=paths,
                            session_id=session_id,
                            revision=3,
                        )
                    )

    def test_dedup_is_scoped_per_session_for_perpetual_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)

            def trigger(session_id: str) -> dict[str, object]:
                args = replace_arg(rich_context_args(repo), "--session-id", session_id)
                result = run_context_handoff(
                    *args,
                    "--stdin-json",
                    stdin=json.dumps(
                        {
                            "session_id": session_id,
                            "prompt": "continue",
                            "contextUsed": 0.31,
                        }
                    ),
                )
                return json_payload(result)

            first_a = trigger("session-a")
            second_a = trigger("session-a")
            first_b = trigger("session-b")

            self.assertTrue(contract_value(first_a, "should_handoff"))
            self.assertFalse(contract_value(second_a, "should_handoff"))
            self.assertTrue(contract_value(second_a, "deduped"))
            self.assertTrue(contract_value(first_b, "should_handoff"))
            self.assertFalse(contract_value(first_b, "deduped"))
            self.assertNotEqual(
                contract_value(first_a, "handoff_scope", "session_scope"),
                contract_value(first_b, "handoff_scope", "session_scope"),
            )
            self.assertNotEqual(
                contract_value(first_a, "capsule_path"),
                contract_value(first_b, "capsule_path"),
            )

    def test_concurrent_same_session_triggers_emit_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            args = replace_arg(rich_context_args(repo), "--session-id", "shared")

            def trigger_once(_index: int) -> dict[str, object]:
                result = run_context_handoff(
                    *args,
                    "--stdin-json",
                    stdin=json.dumps(
                        {
                            "session_id": "shared",
                            "prompt": "continue",
                            "contextUsed": 0.31,
                        }
                    ),
                )
                return json_payload(result)

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(trigger_once, range(6)))

            self.assertEqual(
                sum(bool(contract_value(item, "should_handoff")) for item in results),
                1,
            )
            self.assertEqual(
                sum(bool(contract_value(item, "deduped")) for item in results),
                5,
            )
            self.assertEqual(
                len(list((repo / ".omx/state/relay").rglob("*-handoff.md"))),
                1,
            )


    def test_loads_objective_from_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            session_a = replace_arg(
                rich_handoff_args(repo, session_id="active-a"),
                "--objective",
                "persisted objective A",
            )
            session_a = replace_arg(
                session_a,
                "--next-step",
                "persisted next action A",
            )
            session_b = replace_arg(
                rich_handoff_args(repo, session_id="active-b"),
                "--objective",
                "persisted objective B",
            )
            session_b = replace_arg(
                session_b,
                "--next-step",
                "persisted next action B",
            )
            json_payload(
                run_write_handoff(*session_a, "--update-active-task-only")
            )
            json_payload(
                run_write_handoff(*session_b, "--update-active-task-only")
            )
            result = run_context_handoff(
                "--repo",
                str(repo),
                "--session-id",
                "active-a",
                "--trigger",
                "pre-compact",
                "--dedup-seconds",
                "0",
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "resume_ready"))
            capsule = Path(str(contract_value(payload, "capsule_path")))
            content = capsule.read_text(encoding="utf-8")
            self.assertIn("persisted objective A", content)
            self.assertIn("persisted next action A", content)
            self.assertNotIn("persisted objective B", content)
            self.assertNotIn("persisted next action B", content)

    def test_update_active_task_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            first_args = replace_arg(
                rich_handoff_args(repo, session_id="seed-a"),
                "--objective",
                "seed task A",
            )
            second_args = replace_arg(
                rich_handoff_args(repo, session_id="seed-b"),
                "--objective",
                "seed task B",
            )
            first = json_payload(
                run_write_handoff(*first_args, "--update-active-task-only")
            )
            second = json_payload(
                run_write_handoff(*second_args, "--update-active-task-only")
            )
            first_path = Path(str(contract_value(first, "active_task_path")))
            second_path = Path(str(contract_value(second, "active_task_path")))
            self.assertTrue(first_path.is_file())
            self.assertTrue(second_path.is_file())
            self.assertNotEqual(first_path, second_path)
            self.assertFalse(
                (
                    repo
                    / ".omx/state/relay/.active-task.json"
                ).exists()
            )
            first_task = json.loads(first_path.read_text(encoding="utf-8"))
            second_task = json.loads(second_path.read_text(encoding="utf-8"))
            self.assertEqual(first_task["session_id"], "seed-a")
            self.assertEqual(second_task["session_id"], "seed-b")
            self.assertEqual(first_task["objective"], "seed task A")
            self.assertEqual(second_task["objective"], "seed task B")

    def test_default_out_path_uses_microsecond_stamp(self) -> None:
        repo = Path("/tmp/example-repo")
        first = default_out_path(repo, dt.datetime(2026, 7, 6, 21, 5, 30, 123456))
        second = default_out_path(repo, dt.datetime(2026, 7, 6, 21, 5, 30, 123457))
        self.assertNotEqual(first, second)
        self.assertIn("20260706-210530-123456-handoff.md", str(first))

    def test_prompt_boundary_preserves_mandatory_identity_at_exact_size(self) -> None:
        path = Path("/tmp/complete-capsule-locator.md")
        digest = "b" * 64
        full = build_continuation_prompt(
            path,
            4096,
            session_id="boundary-session",
            revision=9,
            capsule_sha256=digest,
        )
        self.assertIsInstance(full, str)
        assert isinstance(full, str)
        exact_budget = len(full.encode("utf-8"))
        exact = build_continuation_prompt(
            path,
            exact_budget,
            session_id="boundary-session",
            revision=9,
            capsule_sha256=digest,
        )
        too_small = build_continuation_prompt(
            path,
            exact_budget - 1,
            session_id="boundary-session",
            revision=9,
            capsule_sha256=digest,
        )
        self.assertEqual(exact, full)
        self.assertIsNone(too_small)

    def test_live_sized_goal_prompt_fits_the_default_budget(self) -> None:
        prompt = build_continuation_prompt(
            Path(
                "/Users/example/Documents/relay/.omx/state/relay/"
                "sessions/392fc04f4114b8c3/20260726-030809-890000-r10-handoff.md"
            ),
            session_id="019f9d3f-5002-7ad0-b6a1-9ed67eb5c96a",
            revision=10,
            capsule_sha256="f" * 64,
            goal_identity="goal:sha256:" + "e" * 64,
            transfer_nonce="BASVlzsWqDC3pC5Sg6X32vZLjkTuDxVD",
            transfer_id="r10-1da55d9f23cb851e",
            next_action=(
                "Verify the Relay capsule, restore the exact goal if absent, "
                "acknowledge the source, request source stop, and report PASS "
                "with source and destination task IDs."
            ),
            resume_validation_command="git status --short",
            resume_validation_expected="exit 0",
        )
        self.assertIsInstance(prompt, str)
        assert isinstance(prompt, str)
        self.assertLessEqual(len(prompt.encode("utf-8")), 1024)

    def test_minimum_prompt_budget_blocks_delivery_without_invalidating_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            result = run_write_handoff(
                *rich_handoff_args(repo, session_id="minimum-prompt-budget"),
                "--prompt-budget-bytes",
                "128",
            )
            payload = json_payload(result)
            self.assertTrue(contract_value(payload, "resume_ready"))
            self.assertFalse(contract_value(payload, "should_handoff"))
            self.assertFalse(contract_value(payload, "delivery_emitted"))
            self.assertIsNone(contract_value(payload, "continuation_prompt"))
            guard = contract_value(payload, "prompt_guard")
            self.assertIsInstance(guard, dict)
            assert isinstance(guard, dict)
            self.assertFalse(guard["fits"])


@unittest.skipUnless(PACKAGE_ROOT is not None, "portable package source is not installed")
class HookEnvelopeAndParityTests(unittest.TestCase):
    OFFICIAL_COMMON_KEYS = {
        "continue",
        "stopReason",
        "suppressOutput",
        "systemMessage",
        "hookSpecificOutput",
    }

    def test_internal_contract_is_distinct_from_official_hook_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            internal_repo = root / "internal"
            official_repo = root / "official"
            internal_repo.mkdir()
            official_repo.mkdir()
            init_repo(internal_repo)
            init_repo(official_repo)
            install_runtime_fixture(official_repo)
            seed_ready_state(internal_repo, "internal-session")
            seed_ready_state(official_repo, "official-session")

            internal = run_context_handoff(
                "--repo",
                str(internal_repo),
                "--stdin-json",
                stdin=json.dumps(
                    {
                        "session_id": "internal-session",
                        "prompt": "continue",
                        "contextUsed": 0.31,
                    }
                ),
            )
            internal_payload = json_payload(internal)
            self.assertNotIn("hookSpecificOutput", internal_payload)
            self.assertIsNotNone(contract_value(internal_payload, "capsule_path"))
            self.assertTrue(contract_value(internal_payload, "resume_ready"))
            self.assertIsInstance(contract_value(internal_payload, "metrics"), dict)

            official = run_hook(
                PLUGIN_HOOK,
                "UserPromptSubmit",
                official_repo,
                {
                    "session_id": "official-session",
                    "prompt": "continue",
                    "contextUsed": 0.31,
                },
                plugin_root=REPO,
            )
            official_payload = json_payload(official)
            self.assertLessEqual(set(official_payload), self.OFFICIAL_COMMON_KEYS)
            self.assertNotIn("capsule_path", official_payload)
            self.assertNotIn("resume_ready", official_payload)
            self.assertNotIn("metrics", official_payload)
            specific = official_payload.get("hookSpecificOutput")
            self.assertIsInstance(specific, dict)
            assert isinstance(specific, dict)
            self.assertEqual(specific.get("hookEventName"), "UserPromptSubmit")
            additional_context = specific.get("additionalContext")
            self.assertIsInstance(additional_context, str)
            self.assertTrue(additional_context)
            self.assertLessEqual(
                len(str(additional_context).encode("utf-8")),
                1400,
            )

    def test_event_specific_official_envelopes_and_missing_ratio_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            precompact_repo = root / "precompact"
            precompact_repo.mkdir()
            init_repo(precompact_repo)
            install_runtime_fixture(precompact_repo)
            seed_ready_state(precompact_repo, "compact-session")
            precompact = run_hook(
                PLUGIN_HOOK,
                "PreCompact",
                precompact_repo,
                {"session_id": "compact-session"},
                plugin_root=REPO,
            )
            precompact_payload = json_payload(precompact)
            self.assertLessEqual(set(precompact_payload), self.OFFICIAL_COMMON_KEYS)
            self.assertNotIn("hookSpecificOutput", precompact_payload)
            self.assertEqual(
                len(
                    list(
                        (
                            precompact_repo
                            / ".omx/state/relay"
                        ).rglob("*-handoff.md")
                    )
                ),
                1,
            )

            no_ratio_repo = root / "no-ratio"
            no_ratio_repo.mkdir()
            init_repo(no_ratio_repo)
            install_runtime_fixture(no_ratio_repo)
            seed_ready_state(no_ratio_repo, "no-ratio-session")
            no_ratio = run_hook(
                PLUGIN_HOOK,
                "UserPromptSubmit",
                no_ratio_repo,
                {
                    "session_id": "no-ratio-session",
                    "prompt": "ordinary user prompt",
                },
                plugin_root=REPO,
            )
            no_ratio_payload = json_payload(no_ratio)
            self.assertLessEqual(set(no_ratio_payload), self.OFFICIAL_COMMON_KEYS)
            specific = no_ratio_payload.get("hookSpecificOutput")
            if isinstance(specific, dict):
                self.assertNotIn("additionalContext", specific)
            self.assertEqual(
                list(
                    (
                        no_ratio_repo
                        / ".omx/state/relay"
                    ).rglob("*-handoff.md")
                ),
                [],
            )

    def test_plugin_codex_and_workflow_wrappers_have_normalized_parity(self) -> None:
        surfaces = (
            ("plugin", PLUGIN_HOOK, True),
            ("codex", CODEX_HOOK, False),
            ("workflow", WORKFLOW_HOOK, False),
        )
        normalized: dict[str, dict[str, object]] = {}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, wrapper, is_plugin in surfaces:
                repo = root / name
                repo.mkdir()
                init_repo(repo)
                install_runtime_fixture(repo)
                session_id = "parity-session"
                seed_ready_state(repo, session_id)
                result = run_hook(
                    wrapper,
                    "UserPromptSubmit",
                    repo,
                    {
                        "session_id": session_id,
                        "prompt": "continue",
                        "contextUsed": 0.31,
                    },
                    plugin_root=REPO if is_plugin else None,
                )
                payload = json_payload(result)
                self.assertLessEqual(set(payload), self.OFFICIAL_COMMON_KEYS)
                specific = payload.get("hookSpecificOutput")
                self.assertIsInstance(specific, dict)
                assert isinstance(specific, dict)
                prompt = specific.get("additionalContext")
                self.assertIsInstance(prompt, str)

                _pointer_path, pointer = find_latest_pointer(repo)
                capsule_path = contract_value(pointer, "capsule_path")
                self.assertIsInstance(capsule_path, str)
                capsule = Path(str(capsule_path)).read_text(encoding="utf-8")
                self.assertNotIn(str(prompt), capsule)
                self.assertIn("Ship the token-efficient relay package", capsule)

                normalized[name] = {
                    "keys": sorted(payload),
                    "event": specific.get("hookEventName"),
                    "prompt": normalize_transport_text(
                        str(prompt),
                        repo,
                    ),
                    "resume_ready": contract_value(pointer, "resume_ready"),
                    "capsule_budget_bytes": contract_value(
                        pointer,
                        "capsule_budget_bytes",
                    ),
                    "prompt_budget_bytes": contract_value(
                        pointer,
                        "prompt_budget_bytes",
                    ),
                    "delivered": contract_value(
                        pointer,
                        "delivery_emitted",
                        "delivered",
                    ),
                }

        self.assertEqual(normalized["plugin"], normalized["codex"])
        self.assertEqual(normalized["plugin"], normalized["workflow"])


@unittest.skipUnless(PACKAGE_ROOT is not None, "portable package source is not installed")
class InstallAtomicityTests(unittest.TestCase):
    def test_successful_install_enforces_installed_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)

            install = subprocess.run(
                ["bash", str(INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            installed_writer = (
                repo
                / ".agents/skills/relay/scripts/write_handoff.py"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(installed_writer),
                    *rich_handoff_args(repo, session_id="installed-budget"),
                    "--note",
                    "dense multibyte payload " + ("界" * 3000),
                ],
                text=True,
                capture_output=True,
                check=False,
                cwd=repo,
            )
            payload = json_payload(result)
            capsule = Path(str(contract_value(payload, "capsule_path")))
            prompt = str(contract_value(payload, "continuation_prompt", "prompt"))
            self.assertLessEqual(len(capsule.read_bytes()), 4096)
            self.assertLessEqual(len(prompt.encode("utf-8")), 1024)

            for flag, value, expected_error in (
                (
                    "--capsule-budget-bytes",
                    "4757",
                    "capsule byte budget cannot exceed 4096 bytes",
                ),
                (
                    "--prompt-budget-bytes",
                    "4757",
                    "prompt byte budget cannot exceed 1024 bytes",
                ),
            ):
                with self.subTest(installed_flag=flag):
                    oversized_out = repo / f"installed-{flag.removeprefix('--')}.md"
                    oversized = subprocess.run(
                        [
                            sys.executable,
                            str(installed_writer),
                            *rich_handoff_args(
                                repo,
                                session_id=f"installed-oversized-{flag}",
                            ),
                            "--out",
                            str(oversized_out),
                            flag,
                            value,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                        cwd=repo,
                    )
                    self.assertEqual(oversized.returncode, 2)
                    self.assertEqual(oversized.stdout, "")
                    self.assertEqual(oversized.stderr.strip(), expected_error)
                    self.assertFalse(oversized_out.exists())

    def test_reinstall_is_idempotent_and_audits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)

            first_install = subprocess.run(
                ["bash", str(INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
            )
            self.assertEqual(first_install.returncode, 0, first_install.stderr)
            first_audit = subprocess.run(
                ["bash", str(AUDIT_INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
            )
            self.assertEqual(first_audit.returncode, 0, first_audit.stderr)
            installed_skill = repo / ".agents/skills/relay"
            installed_hook = repo / "scripts/workflow/relay_hook.sh"
            first_bytes = {
                str(path.relative_to(repo)): path.read_bytes()
                for path in sorted(installed_skill.rglob("*"))
                if path.is_file()
            }
            first_bytes[str(installed_hook.relative_to(repo))] = (
                installed_hook.read_bytes()
            )

            reinstall = subprocess.run(
                ["bash", str(INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
            )
            self.assertEqual(reinstall.returncode, 0, reinstall.stderr)
            second_audit = subprocess.run(
                ["bash", str(AUDIT_INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
            )
            self.assertEqual(second_audit.returncode, 0, second_audit.stderr)
            second_bytes = {
                str(path.relative_to(repo)): path.read_bytes()
                for path in sorted(installed_skill.rglob("*"))
                if path.is_file()
            }
            second_bytes[str(installed_hook.relative_to(repo))] = (
                installed_hook.read_bytes()
            )
            self.assertEqual(second_bytes, first_bytes)

    def test_failed_canonical_staging_preserves_existing_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            target_skill = repo / ".agents/skills/relay"
            target_skill.mkdir(parents=True)
            skill_marker = target_skill / "existing-marker.bin"
            skill_marker.write_bytes(b"existing canonical skill\n")
            target_hook = repo / "scripts/workflow/relay_hook.sh"
            target_hook.parent.mkdir(parents=True)
            target_hook.write_bytes(b"existing canonical hook\n")

            stub_dir = Path(tmp) / "bin"
            stub_dir.mkdir()
            cp_stub = stub_dir / "cp"
            cp_stub.write_text("#!/usr/bin/env bash\nexit 97\n", encoding="utf-8")
            cp_stub.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
            install = subprocess.run(
                ["bash", str(INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
                env=env,
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertEqual(skill_marker.read_bytes(), b"existing canonical skill\n")
            self.assertEqual(target_hook.read_bytes(), b"existing canonical hook\n")

    def test_fresh_repo_staging_failure_removes_created_namespace_and_stage_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            stub_dir = Path(tmp) / "bin"
            stub_dir.mkdir()
            cp_stub = stub_dir / "cp"
            cp_stub.write_text("#!/usr/bin/env bash\nexit 97\n", encoding="utf-8")
            cp_stub.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"

            install = subprocess.run(
                ["bash", str(INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
                env=env,
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertFalse((repo / ".agents").exists())
            self.assertFalse((repo / "scripts").exists())
            self.assertEqual(
                list(repo.rglob(".relay-install.*")),
                [],
            )
            self.assertFalse((repo / ".omx").exists())

    def test_install_rejects_symlinked_mutation_namespaces_without_outside_writes(self) -> None:
        def snapshot(root: Path) -> dict[str, tuple[str, bytes | str]]:
            result: dict[str, tuple[str, bytes | str]] = {}
            for current, directories, files in os.walk(root, followlinks=False):
                current_path = Path(current)
                for name in sorted([*directories, *files]):
                    path = current_path / name
                    relative = str(path.relative_to(root))
                    if path.is_symlink():
                        result[relative] = ("symlink", os.readlink(path))
                    elif path.is_dir():
                        result[relative] = ("directory", "")
                    else:
                        result[relative] = ("file", path.read_bytes())
            return result

        for relative in (".agents", "scripts", "scripts/workflow", ".omx"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo = root / "repo"
                outside = root / "outside"
                repo.mkdir()
                outside.mkdir()
                init_repo(repo)
                destination = outside / relative.replace("/", "-").lstrip(".")
                destination.mkdir()
                (destination / "sentinel.bin").write_bytes(b"outside must remain unchanged\n")
                link = repo / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(destination, target_is_directory=True)
                repo_before = snapshot(repo)
                outside_before = snapshot(outside)

                install = subprocess.run(
                    ["bash", str(INSTALL), str(repo)],
                    text=True,
                    capture_output=True,
                    check=False,
                    cwd=REPO,
                )

                self.assertNotEqual(install.returncode, 0)
                self.assertEqual(snapshot(outside), outside_before)
                self.assertEqual(snapshot(repo), repo_before)
                self.assertEqual(
                    list(repo.rglob(".relay-install.*")),
                    [],
                )

    def test_failed_hook_swap_rolls_back_both_canonical_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            init_repo(repo)
            target_skill = repo / ".agents/skills/relay"
            target_skill.mkdir(parents=True)
            skill_marker = target_skill / "existing-marker.bin"
            skill_marker.write_bytes(b"existing canonical skill\n")
            target_hook = repo / "scripts/workflow/relay_hook.sh"
            target_hook.parent.mkdir(parents=True)
            target_hook.write_bytes(b"existing canonical hook\n")

            env = os.environ.copy()
            env["RELAY_INSTALL_FAULT"] = "canonical_hook_swap"
            install = subprocess.run(
                ["bash", str(INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
                env=env,
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertEqual(skill_marker.read_bytes(), b"existing canonical skill\n")
            self.assertEqual(target_hook.read_bytes(), b"existing canonical hook\n")

    def test_failure_after_finalize_rolls_back_canonical_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            target_skill = repo / ".agents/skills/relay"
            target_skill.mkdir(parents=True)
            skill_marker = target_skill / "existing-marker.bin"
            skill_marker.write_bytes(b"existing canonical skill\n")
            target_hook = repo / "scripts/workflow/relay_hook.sh"
            target_hook.parent.mkdir(parents=True)
            target_hook.write_bytes(b"existing canonical hook\n")

            env = os.environ.copy()
            env["RELAY_INSTALL_FAULT"] = "canonical_finalize"
            install = subprocess.run(
                ["bash", str(INSTALL), str(repo)],
                text=True,
                capture_output=True,
                check=False,
                cwd=REPO,
                env=env,
            )

            self.assertNotEqual(install.returncode, 0)
            self.assertEqual(skill_marker.read_bytes(), b"existing canonical skill\n")
            self.assertEqual(target_hook.read_bytes(), b"existing canonical hook\n")
            self.assertEqual(
                list((repo / ".agents").glob(".relay-install.*")),
                [],
            )


class GoalTelemetryReportTests(unittest.TestCase):
    @staticmethod
    def v2_document(*, pair_count: int = 20) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        for pair in range(1, pair_count + 1):
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
                        "capsule_bytes": 1800 if condition == "current_canonical" else 1200,
                        "prompt_bytes": 400 if condition == "current_canonical" else 300,
                    }
                )
        return {
            "schema_version": 2,
            "study_type": "token_efficient_relay_v2",
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

    @classmethod
    def v3_document(cls, *, pair_count: int = 20) -> dict[str, object]:
        document = cls.v2_document(pair_count=pair_count)
        document["schema_version"] = 3
        document["study_type"] = "token_efficient_relay_v3"
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

        diagnostic = json.loads(json.dumps(rows[-1]))
        diagnostic.update(
            {
                "paired_run_id": "diagnostic-middle-01",
                "task_id": "diagnostic-middle-task-01",
                "condition": "diagnostic_lost_in_middle",
                "tokensUsed": 150,
                "qualifies_as_claim_evidence": False,
                "source_tokens_before_handoff": 60,
                "handoff_generation_tokens": 10,
                "destination_resume_tokens": 20,
                "completion_tokens_after_resume": 60,
            }
        )
        diagnostic_quality = diagnostic["continuation_quality"]
        assert isinstance(diagnostic_quality, dict)
        diagnostic_quality["middle_critical_fact_recovered"] = False
        rows.append(diagnostic)
        return document

    def test_build_report_preserves_exact_samples_and_labels_costs_as_sensitivity(self) -> None:
        report = build_report(
            [1824, 1863, 1863, 1826, 2162],
            [2017, 2038, 1960, 2006, 2033],
            model="gpt-5.6-sol",
            cached_input_rate=0.50,
            input_rate=5.00,
            output_rate=30.00,
        )
        self.assertEqual(report["sample_size"], 5)
        self.assertEqual(report["paired_savings_tokens"], [193, 175, 97, 180, -129])
        self.assertEqual(report["clean_lower_pair_count"], 4)
        self.assertEqual(report["clean_total_tokens"], 9538)
        self.assertEqual(report["fork_total_tokens"], 10054)
        self.assertAlmostEqual(report["mean_token_reduction_percent"], 5.1322856572)
        self.assertAlmostEqual(report["paired_savings_sample_stdev_tokens"], 135.159905297)
        self.assertAlmostEqual(report["paired_savings_standard_error_tokens"], 60.445347215)
        self.assertEqual(report["paired_sign_test_two_sided_p_value"], 0.375)
        self.assertEqual(
            report["efficiency_verdict"],
            "no_statistically_stable_goal_token_improvement",
        )
        self.assertAlmostEqual(
            report["cost_sensitivity"]["all_output"]["savings_usd"],
            0.01548,
        )
        self.assertAlmostEqual(
            report["cost_sensitivity"]["all_uncached_input"][
                "median_paired_savings_usd"
            ],
            0.000875,
        )
        self.assertIn("not exact Codex charges", report["cost_warning"])

    def test_build_report_rejects_unpaired_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "same length"):
            build_report(
                [100],
                [100, 101],
                model="test",
                cached_input_rate=1,
                input_rate=2,
                output_rate=3,
            )

    def test_v2_report_passes_only_with_quality_and_stable_token_direction(self) -> None:
        report = telemetry.build_v2_report(self.v2_document())
        self.assertEqual(report["sample_size_pairs"], 20)
        self.assertEqual(report["row_count"], 40)
        self.assertEqual(report["median_paired_savings_tokens"], 100.0)
        self.assertEqual(report["candidate_lower_token_pair_count"], 20)
        self.assertEqual(report["candidate_higher_token_pair_count"], 0)
        self.assertLess(report["paired_sign_test_two_sided_p_value"], 0.05)
        self.assertTrue(report["quality_gate"]["all_pairs_noninferior"])
        self.assertTrue(report["quality_gate"]["no_correctness_regression"])
        self.assertTrue(report["quality_gate"]["no_constraint_regression"])
        self.assertTrue(report["quality_gate"]["candidate_all_passed_and_ready"])
        self.assertEqual(report["conditions"]["current_canonical"]["readiness_rate"], 1.0)
        self.assertEqual(report["conditions"]["candidate"]["failure_rate"], 0.0)
        self.assertEqual(report["bytes"]["candidate"]["capsule_total"], 24_000)
        self.assertEqual(report["bytes"]["candidate"]["prompt_total"], 6_000)
        self.assertTrue(report["empirical_gate_passed"])
        self.assertTrue(report["token_efficiency_claim_ready"])
        self.assertFalse(report["cost_claim_ready"])

    def test_v2_adjusted_quality_zero_preserves_failure_and_readiness_denominators(self) -> None:
        document = self.v2_document()
        rows = document["rows"]
        assert isinstance(rows, list)
        candidate = next(
            row
            for row in rows
            if row["paired_run_id"] == "pair-01" and row["condition"] == "candidate"
        )
        candidate["outcome"] = "failed"
        candidate["resume_ready"] = False
        report = telemetry.build_v2_report(document)
        pair = next(
            row for row in report["pairs"] if row["paired_run_id"] == "pair-01"
        )
        self.assertAlmostEqual(pair["candidate"]["raw_quality"], 100.0)
        self.assertEqual(pair["candidate"]["adjusted_quality"], 0.0)
        self.assertFalse(report["quality_gate"]["all_pairs_noninferior"])
        self.assertEqual(report["conditions"]["candidate"]["failure_count"], 1)
        self.assertEqual(report["conditions"]["candidate"]["failure_rate"], 0.05)
        self.assertEqual(report["conditions"]["candidate"]["readiness_rate"], 0.95)
        self.assertFalse(report["empirical_gate_passed"])

    def test_v2_all_failed_nonready_candidates_never_pass_empirical_gate(self) -> None:
        document = self.v2_document()
        rows = document["rows"]
        assert isinstance(rows, list)
        for row in rows:
            row["outcome"] = "failed"
            row["resume_ready"] = False
        report = telemetry.build_v2_report(document)
        self.assertTrue(report["quality_gate"]["all_pairs_noninferior"])
        self.assertFalse(report["quality_gate"]["candidate_all_passed_and_ready"])
        self.assertFalse(report["empirical_gate_passed"])
        self.assertFalse(report["token_efficiency_claim_ready"])

    def test_v2_schema_requires_unique_tasks_and_bound_distinct_runtimes(self) -> None:
        repeated_tasks = self.v2_document()
        rows = repeated_tasks["rows"]
        assert isinstance(rows, list)
        for row in rows:
            row["task_id"] = "one-repeated-task"
        with self.assertRaisesRegex(ValueError, "20 unique task_ids"):
            telemetry.validate_study_document(repeated_tasks)

        same_commit = self.v2_document()
        same_commit["candidate_commit_id"] = same_commit["control_commit_id"]
        with self.assertRaisesRegex(ValueError, "commit IDs must be distinct"):
            telemetry.validate_study_document(same_commit)

        same_runtime = self.v2_document()
        same_runtime["candidate_runtime_sha256"] = same_runtime[
            "control_runtime_sha256"
        ]
        with self.assertRaisesRegex(ValueError, "runtime SHA-256 values must be distinct"):
            telemetry.validate_study_document(same_runtime)

    def test_v2_schema_rejects_incomplete_pairs_bad_scores_and_small_studies(self) -> None:
        small = self.v2_document(pair_count=19)
        with self.assertRaisesRegex(ValueError, "at least 20 paired runs"):
            telemetry.validate_study_document(small)

        incomplete = self.v2_document()
        rows = incomplete["rows"]
        assert isinstance(rows, list)
        rows.pop()
        with self.assertRaisesRegex(ValueError, "exactly one row per condition"):
            telemetry.validate_study_document(incomplete)

        bad_score = self.v2_document()
        bad_rows = bad_score["rows"]
        assert isinstance(bad_rows, list)
        quality = bad_rows[0]["quality"]
        assert isinstance(quality, dict)
        quality["task_correctness"] = 3
        with self.assertRaisesRegex(ValueError, "quality scores must be integers from 0 to 2"):
            telemetry.validate_study_document(bad_score)

    def test_v2_schema_requires_safe_prereg_paths_and_post_freeze_run_starts(self) -> None:
        unsafe_path = self.v2_document()
        preregistration = unsafe_path["preregistration"]
        assert isinstance(preregistration, dict)
        preregistration["task_set_path"] = "../tasks.json"
        with self.assertRaisesRegex(ValueError, "safe repository-relative POSIX path"):
            telemetry.validate_study_document(unsafe_path)

        nul_path = self.v2_document()
        preregistration = nul_path["preregistration"]
        assert isinstance(preregistration, dict)
        preregistration["task_set_path"] = "artifacts/tasks\0.json"
        with self.assertRaisesRegex(ValueError, "safe repository-relative POSIX path"):
            telemetry.validate_study_document(nul_path)

        duplicate_path = self.v2_document()
        preregistration = duplicate_path["preregistration"]
        assert isinstance(preregistration, dict)
        preregistration["rubric_path"] = preregistration["task_set_path"]
        with self.assertRaisesRegex(ValueError, "paths must be distinct"):
            telemetry.validate_study_document(duplicate_path)

        naive_start = self.v2_document()
        rows = naive_start["rows"]
        assert isinstance(rows, list)
        rows[0]["run_started_at"] = "2026-07-02T00:00:00"
        with self.assertRaisesRegex(ValueError, "offset-aware ISO-8601"):
            telemetry.validate_study_document(naive_start)

        pre_freeze = self.v2_document()
        rows = pre_freeze["rows"]
        assert isinstance(rows, list)
        rows[0]["run_started_at"] = "2026-06-30T23:59:59-00:00"
        with self.assertRaisesRegex(ValueError, "after preregistration frozen_at"):
            telemetry.validate_study_document(pre_freeze)

        equal_freeze = self.v2_document()
        rows = equal_freeze["rows"]
        assert isinstance(rows, list)
        rows[0]["run_started_at"] = "2026-06-30T20:00:00-04:00"
        with self.assertRaisesRegex(ValueError, "after preregistration frozen_at"):
            telemetry.validate_study_document(equal_freeze)

        future_start = self.v2_document()
        rows = future_start["rows"]
        assert isinstance(rows, list)
        rows[0]["run_started_at"] = "2999-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "must not be in the future"):
            telemetry.validate_study_document(future_start)

    def test_v3_aggregate_chain_report_and_diagnostic_isolation(self) -> None:
        document = self.v3_document()
        report = telemetry.build_v3_report(document)

        self.assertEqual(report["schema_version"], 3)
        self.assertTrue(report["qualifies_as_v3_evidence"])
        self.assertEqual(report["sample_size_pairs"], 20)
        self.assertEqual(report["claim_row_count"], 40)
        self.assertEqual(report["row_count"], 41)
        self.assertEqual(len(report["pairs"]), 20)
        self.assertNotIn(
            "diagnostic-middle-01",
            {pair["paired_run_id"] for pair in report["pairs"]},
        )
        self.assertEqual(report["median_paired_savings_tokens"], 100.0)
        self.assertLess(report["paired_sign_test_two_sided_p_value"], 0.05)

        candidate = report["conditions"]["candidate"]
        self.assertEqual(candidate["tokensUsed_total"], 2_210)
        self.assertEqual(
            candidate["chain_tokens"],
            {
                "source_tokens_before_handoff_total": 800,
                "handoff_generation_tokens_total": 200,
                "destination_resume_tokens_total": 400,
                "completion_tokens_after_resume_total": 810,
            },
        )
        observability = candidate["observability"]
        self.assertEqual(observability["handoff_count_total"], 20)
        self.assertEqual(observability["post_ack_source_tokens_total"], 0)
        self.assertEqual(observability["post_ack_source_actions_total"], 0)
        self.assertEqual(
            observability["acknowledgement_outcomes"],
            {"accepted": 20},
        )
        self.assertEqual(
            observability["source_stop_capabilities"],
            {"native_interrupt": 20},
        )
        self.assertEqual(observability["source_stop_outcomes"], {"interrupted": 20})
        self.assertEqual(observability["transfer_latency_ms"]["count"], 20)
        self.assertEqual(observability["human_intervention_count"], 0)
        for check in (
            "next_step_correct",
            "constraints_retained",
            "no_completed_work_repeated",
            "no_remaining_work_skipped",
            "repository_goal_reconciled",
            "validation_sufficient",
            "middle_critical_fact_recovered",
        ):
            self.assertEqual(
                candidate["continuation_quality"][check],
                {"passed_count": 20, "pass_rate": 1.0},
            )

        self.assertEqual(report["diagnostics"]["row_count"], 1)
        diagnostic = report["diagnostics"]["conditions"][
            "diagnostic_lost_in_middle"
        ]
        self.assertFalse(diagnostic["qualifies_as_claim_evidence"])
        self.assertEqual(diagnostic["row_count"], 1)
        self.assertEqual(
            diagnostic["continuation_quality"]["middle_critical_fact_recovered"],
            {"passed_count": 0, "pass_rate": 0.0},
        )
        self.assertTrue(
            report["quality_gate"]["candidate_zero_post_ack_source_activity"]
        )
        self.assertTrue(report["empirical_gate_passed"])

    def test_v3_rejects_invalid_chain_and_post_ack_activity(self) -> None:
        invalid_chain = self.v3_document()
        rows = invalid_chain["rows"]
        assert isinstance(rows, list)
        rows[0]["completion_tokens_after_resume"] = 0
        with self.assertRaisesRegex(ValueError, "tokensUsed must equal four chain components"):
            telemetry.validate_v3_study_document(invalid_chain)

        negative_count = self.v3_document()
        rows = negative_count["rows"]
        assert isinstance(rows, list)
        rows[0]["ownership_conflict_count"] = -1
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            telemetry.validate_v3_study_document(negative_count)

        nonfinite_latency = self.v3_document()
        rows = nonfinite_latency["rows"]
        assert isinstance(rows, list)
        rows[0]["acknowledgement_latency_ms"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite non-negative number"):
            telemetry.validate_v3_study_document(nonfinite_latency)

        impossible_attempts = self.v3_document()
        rows = impossible_attempts["rows"]
        assert isinstance(rows, list)
        rows[0]["source_stop_failure_count"] = 2
        with self.assertRaisesRegex(ValueError, "failure count cannot exceed attempt count"):
            telemetry.validate_v3_study_document(impossible_attempts)

        independent_retries = self.v3_document()
        rows = independent_retries["rows"]
        assert isinstance(rows, list)
        rows[0]["retry_count"] = 3
        telemetry.validate_v3_study_document(independent_retries)

        impossible_recovery = self.v3_document()
        rows = impossible_recovery["rows"]
        assert isinstance(rows, list)
        rows[0]["termination_pending_recovered"] = True
        with self.assertRaisesRegex(ValueError, "recovery requires termination pending observation"):
            telemetry.validate_v3_study_document(impossible_recovery)

        claim_opt_out = self.v3_document()
        rows = claim_opt_out["rows"]
        assert isinstance(rows, list)
        rows[0]["qualifies_as_claim_evidence"] = False
        with self.assertRaisesRegex(ValueError, "claim conditions must qualify as claim evidence"):
            telemetry.validate_v3_study_document(claim_opt_out)

        bad_stop_pair = self.v3_document()
        rows = bad_stop_pair["rows"]
        assert isinstance(rows, list)
        rows[0]["source_stop_outcome"] = "quiesced"
        with self.assertRaisesRegex(ValueError, "incompatible source stop capability and outcome"):
            telemetry.validate_v3_study_document(bad_stop_pair)

        unexpected_key = self.v3_document()
        rows = unexpected_key["rows"]
        assert isinstance(rows, list)
        rows[0]["completion_after_resume_tokens"] = rows[0].pop(
            "completion_tokens_after_resume"
        )
        with self.assertRaisesRegex(ValueError, "v3 row schema mismatch"):
            telemetry.validate_v3_study_document(unexpected_key)

        post_ack_activity = self.v3_document()
        rows = post_ack_activity["rows"]
        assert isinstance(rows, list)
        candidate = next(
            row
            for row in rows
            if row["paired_run_id"] == "pair-01" and row["condition"] == "candidate"
        )
        candidate["post_ack_source_tokens"] = 1
        with self.assertRaisesRegex(ValueError, "passing candidate post-ack source activity"):
            telemetry.validate_v3_study_document(post_ack_activity)

        valid_no_handoff = self.v3_document()
        rows = valid_no_handoff["rows"]
        assert isinstance(rows, list)
        diagnostic = rows[-1]
        diagnostic.update(
            {
                "handoff_count": 0,
                "handoff_generation_tokens": 0,
                "destination_resume_tokens": 0,
                "completion_tokens_after_resume": 0,
                "source_tokens_before_handoff": diagnostic["tokensUsed"],
                "transfer_latency_ms": None,
                "acknowledgement_outcome": "not_applicable",
                "acknowledgement_latency_ms": None,
                "acknowledgement_attempt_count": 0,
                "acknowledgement_failure_count": 0,
                "source_stop_capability": "not_applicable",
                "source_stop_outcome": "not_applicable",
                "source_stop_latency_ms": None,
                "source_stop_attempt_count": 0,
                "source_stop_failure_count": 0,
                "retry_count": 0,
                "post_ack_source_tokens": 0,
                "post_ack_source_actions": 0,
                "termination_pending_observed": False,
                "termination_pending_recovered": False,
            }
        )
        telemetry.validate_v3_study_document(valid_no_handoff)

        bad_no_handoff = json.loads(json.dumps(valid_no_handoff))
        rows = bad_no_handoff["rows"]
        assert isinstance(rows, list)
        diagnostic = rows[-1]
        diagnostic["acknowledgement_outcome"] = "accepted"
        with self.assertRaisesRegex(ValueError, "zero-handoff outcomes must be not_applicable"):
            telemetry.validate_v3_study_document(bad_no_handoff)

        failed_candidate = self.v3_document()
        rows = failed_candidate["rows"]
        assert isinstance(rows, list)
        candidate = next(
            row
            for row in rows
            if row["paired_run_id"] == "pair-01" and row["condition"] == "candidate"
        )
        candidate.update(
            {
                "outcome": "failed",
                "resume_ready": False,
                "post_ack_source_tokens": 5,
                "post_ack_source_actions": 1,
                "source_stop_outcome": "termination_pending",
                "termination_pending_observed": True,
                "human_intervention_count": 1,
            }
        )
        report = telemetry.build_v3_report(failed_candidate)
        candidate_summary = report["conditions"]["candidate"]
        self.assertEqual(candidate_summary["failure_count"], 1)
        self.assertEqual(candidate_summary["readiness_count"], 19)
        self.assertEqual(
            candidate_summary["observability"]["post_ack_source_tokens_total"],
            5,
        )
        self.assertEqual(
            candidate_summary["observability"]["termination_pending_observed_count"],
            1,
        )
        self.assertEqual(
            candidate_summary["observability"]["human_intervention_count"],
            1,
        )
        self.assertFalse(report["empirical_gate_passed"])

    def test_v3_recovered_termination_pending_is_not_an_unresolved_gate_failure(self) -> None:
        document = self.v3_document()
        rows = document["rows"]
        assert isinstance(rows, list)
        candidate = next(
            row
            for row in rows
            if row["paired_run_id"] == "pair-01" and row["condition"] == "candidate"
        )
        candidate["termination_pending_observed"] = True
        candidate["termination_pending_recovered"] = True
        report = telemetry.build_v3_report(document)
        self.assertTrue(
            report["quality_gate"]["candidate_no_unresolved_termination_pending"]
        )
        self.assertTrue(report["empirical_gate_passed"])

    def test_study_json_cli_dispatches_exact_v2_and_v3_schema_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            study_path = Path(tmp) / "study.json"
            for document, expected_schema in (
                (self.v2_document(), 2),
                (self.v3_document(), 3),
            ):
                study_path.write_text(json.dumps(document), encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(GOAL_TELEMETRY_REPORT), "--study-json", str(study_path)],
                    cwd=REPO,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["schema_version"], expected_schema)

            mixed = self.v3_document()
            mixed["study_type"] = "token_efficient_relay_v2"
            study_path.write_text(json.dumps(mixed), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(GOAL_TELEMETRY_REPORT), "--study-json", str(study_path)],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mixed or partial markers are invalid", result.stderr)

    def test_legacy_aggregate_report_is_explicitly_not_v2_evidence(self) -> None:
        report = build_report(
            [100, 90],
            [110, 100],
            model="legacy",
            cached_input_rate=1,
            input_rate=2,
            output_rate=3,
        )
        self.assertEqual(report["evidence_schema"], "legacy_clean_fork_aggregate_v1")
        self.assertFalse(report["qualifies_as_v2_evidence"])


@unittest.skipUnless(PACKAGE_ROOT is not None, "portable package source is not installed")
class ArtifactHygieneTests(unittest.TestCase):
    def test_committed_artifacts_are_sanitized_samples_only(self) -> None:
        assert PACKAGE_ROOT is not None
        artifacts = PACKAGE_ROOT / "artifacts"
        self.assertTrue((artifacts / "handoffs" / "sanitized-sample-handoff.md").exists())
        handoffs = sorted((artifacts / "handoffs").glob("*.md"))
        self.assertEqual(
            [path.name for path in handoffs],
            ["sanitized-sample-handoff.md"],
        )


if __name__ == "__main__":
    unittest.main()
