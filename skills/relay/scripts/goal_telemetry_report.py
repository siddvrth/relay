#!/usr/bin/env python3
"""Validate and summarize paired goal-period handoff telemetry."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path, PurePosixPath
import re
import statistics
from typing import Any, Mapping, Sequence


V2_SCHEMA_VERSION = 2
V2_STUDY_TYPE = "token_efficient_relay_v2"
V2_TELEMETRY_SCOPE = "exact_goal_period_tokensUsed"
CONTROL_CONDITION = "current_canonical"
CANDIDATE_CONDITION = "candidate"
MINIMUM_PAIRED_RUNS = 20
SIGN_TEST_ALPHA = 0.05
RUBRIC_WEIGHTS: dict[str, float] = {
    "task_correctness": 0.40,
    "constraint_adherence": 0.20,
    "completeness": 0.15,
    "validation_evidence": 0.15,
    "resume_usefulness": 0.10,
}
OUTCOMES = {"passed", "failed", "blocked"}
V3_SCHEMA_VERSION = 3
V3_STUDY_TYPE = "token_efficient_relay_v3"

DOCUMENT_KEYS = {
    "schema_version",
    "study_type",
    "telemetry_scope",
    "control_condition",
    "candidate_condition",
    "control_commit_id",
    "candidate_commit_id",
    "control_runtime_sha256",
    "candidate_runtime_sha256",
    "model",
    "reasoning_effort",
    "repository",
    "goal_token_budget",
    "task_set_id",
    "randomization_plan",
    "sign_test_alpha",
    "preregistration",
    "rubric",
    "rows",
}
PREREGISTRATION_KEYS = {
    "frozen_at",
    "task_set_path",
    "task_set_sha256",
    "randomization_plan_path",
    "randomization_plan_sha256",
    "rubric_path",
    "rubric_sha256",
    "analysis_plan_path",
    "analysis_plan_sha256",
}
ROW_KEYS = {
    "paired_run_id",
    "task_id",
    "condition",
    "run_started_at",
    "tokensUsed",
    "outcome",
    "resume_ready",
    "quality",
    "rubric_notes",
    "capsule_bytes",
    "prompt_bytes",
}


def parse_token_list(value: str) -> list[int]:
    try:
        tokens = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("token lists must contain integers") from exc
    if not tokens or any(token < 0 for token in tokens):
        raise argparse.ArgumentTypeError("token lists must contain non-negative integers")
    return tokens


def token_cost(tokens: float, rate_per_million: float) -> float:
    return tokens * rate_per_million / 1_000_000


def two_sided_sign_test_p_value(positive: int, negative: int) -> float:
    """Two-sided binomial sign-test p-value; ties excluded."""
    trials = positive + negative
    if trials == 0:
        return 1.0
    tail = min(positive, negative)
    probability = sum(math.comb(trials, count) for count in range(tail + 1)) / (2**trials)
    return min(1.0, 2 * probability)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise ValueError(f"{label} schema mismatch: {'; '.join(details)}")


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _valid_commit_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{7,64}", value) is not None


def _parse_offset_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _valid_repo_relative_path(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\0" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and bool(path.parts)
        and value == path.as_posix()
        and all(part not in {"", ".", ".."} for part in path.parts)
        and ".git" not in path.parts
    )


def validate_study_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Check the v2 study schema and return paired row indexes."""
    if not isinstance(document, Mapping):
        raise ValueError("v2 study document must be an object")
    _require_exact_keys(document, DOCUMENT_KEYS, "v2 study document")

    if document.get("schema_version") != V2_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {V2_SCHEMA_VERSION}")
    if document.get("study_type") != V2_STUDY_TYPE:
        raise ValueError(f"study_type must be {V2_STUDY_TYPE}")
    if document.get("telemetry_scope") != V2_TELEMETRY_SCOPE:
        raise ValueError(f"telemetry_scope must be {V2_TELEMETRY_SCOPE}")
    if document.get("control_condition") != CONTROL_CONDITION:
        raise ValueError(f"control_condition must be {CONTROL_CONDITION}")
    if document.get("candidate_condition") != CANDIDATE_CONDITION:
        raise ValueError(f"candidate_condition must be {CANDIDATE_CONDITION}")
    control_commit = document.get("control_commit_id")
    candidate_commit = document.get("candidate_commit_id")
    if not _valid_commit_id(control_commit) or not _valid_commit_id(candidate_commit):
        raise ValueError("control and candidate commit IDs must be non-empty hexadecimal IDs")
    if control_commit == candidate_commit:
        raise ValueError("control and candidate commit IDs must be distinct")
    control_runtime = document.get("control_runtime_sha256")
    candidate_runtime = document.get("candidate_runtime_sha256")
    if not _valid_sha256(control_runtime) or not _valid_sha256(candidate_runtime):
        raise ValueError("control and candidate runtime SHA-256 values must be valid")
    if control_runtime == candidate_runtime:
        raise ValueError("control and candidate runtime SHA-256 values must be distinct")
    for key in (
        "model",
        "reasoning_effort",
        "repository",
        "task_set_id",
        "randomization_plan",
    ):
        if not _nonempty_string(document.get(key)):
            raise ValueError(f"{key} must be a non-empty string")
    if not _nonnegative_int(document.get("goal_token_budget")) or document["goal_token_budget"] == 0:
        raise ValueError("goal_token_budget must be a positive integer")
    if document.get("sign_test_alpha") != SIGN_TEST_ALPHA:
        raise ValueError(f"sign_test_alpha must be frozen at {SIGN_TEST_ALPHA}")

    preregistration = document.get("preregistration")
    if not isinstance(preregistration, Mapping):
        raise ValueError("preregistration must be an object")
    _require_exact_keys(preregistration, PREREGISTRATION_KEYS, "preregistration")
    frozen_at = _parse_offset_timestamp(preregistration.get("frozen_at"))
    if frozen_at is None:
        raise ValueError("preregistration frozen_at must be an offset-aware ISO-8601 timestamp")
    preregistration_paths: list[str] = []
    for stem in ("task_set", "randomization_plan", "rubric", "analysis_plan"):
        path_key = f"{stem}_path"
        digest_key = f"{stem}_sha256"
        if not _valid_repo_relative_path(preregistration.get(path_key)):
            raise ValueError(
                f"preregistration {path_key} must be a safe repository-relative POSIX path"
            )
        preregistration_paths.append(str(preregistration[path_key]))
        if not _valid_sha256(preregistration.get(digest_key)):
            raise ValueError(f"preregistration {digest_key} must be a SHA-256 value")
    if len(set(preregistration_paths)) != len(preregistration_paths):
        raise ValueError("preregistration artifact paths must be distinct")

    rubric = document.get("rubric")
    if not isinstance(rubric, Mapping):
        raise ValueError("rubric must be an object")
    _require_exact_keys(rubric, set(RUBRIC_WEIGHTS), "rubric")
    for dimension, expected_weight in RUBRIC_WEIGHTS.items():
        if rubric.get(dimension) != expected_weight:
            raise ValueError(
                f"rubric weight for {dimension} must be frozen at {expected_weight}"
            )

    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")

    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"row {index} must be an object")
        _require_exact_keys(row, ROW_KEYS, f"row {index}")
        for key in ("paired_run_id", "task_id", "rubric_notes"):
            if not _nonempty_string(row.get(key)):
                raise ValueError(f"row {index} {key} must be a non-empty string")
        run_started_at = _parse_offset_timestamp(row.get("run_started_at"))
        if run_started_at is None:
            raise ValueError(
                f"row {index} run_started_at must be an offset-aware ISO-8601 timestamp"
            )
        if run_started_at <= frozen_at:
            raise ValueError(
                f"row {index} run_started_at must be after preregistration frozen_at"
            )
        if run_started_at.astimezone(dt.timezone.utc) > (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        ):
            raise ValueError(f"row {index} run_started_at must not be in the future")
        condition = row.get("condition")
        if condition not in {CONTROL_CONDITION, CANDIDATE_CONDITION}:
            raise ValueError(f"row {index} condition must be a frozen condition label")
        if not _nonnegative_int(row.get("tokensUsed")):
            raise ValueError(f"row {index} tokensUsed must be a non-negative integer")
        if row.get("outcome") not in OUTCOMES:
            raise ValueError(f"row {index} outcome must be passed, failed, or blocked")
        if not isinstance(row.get("resume_ready"), bool):
            raise ValueError(f"row {index} resume_ready must be boolean")
        for key in ("capsule_bytes", "prompt_bytes"):
            if not _nonnegative_int(row.get(key)):
                raise ValueError(f"row {index} {key} must be a non-negative integer")

        quality = row.get("quality")
        if not isinstance(quality, Mapping):
            raise ValueError(f"row {index} quality must be an object")
        _require_exact_keys(quality, set(RUBRIC_WEIGHTS), f"row {index} quality")
        if any(
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= 2
            for score in quality.values()
        ):
            raise ValueError("quality scores must be integers from 0 to 2")

        paired_run_id = str(row["paired_run_id"])
        condition_rows = pairs.setdefault(paired_run_id, {})
        if condition in condition_rows:
            raise ValueError(
                f"paired run {paired_run_id} must contain exactly one row per condition"
            )
        condition_rows[str(condition)] = row

    if len(pairs) < MINIMUM_PAIRED_RUNS:
        raise ValueError(f"v2 evidence requires at least {MINIMUM_PAIRED_RUNS} paired runs")
    for paired_run_id, condition_rows in pairs.items():
        if set(condition_rows) != {CONTROL_CONDITION, CANDIDATE_CONDITION}:
            raise ValueError(
                f"paired run {paired_run_id} must contain exactly one row per condition"
            )
        if condition_rows[CONTROL_CONDITION]["task_id"] != condition_rows[CANDIDATE_CONDITION]["task_id"]:
            raise ValueError(f"paired run {paired_run_id} must share one task_id")
    task_ids = {
        str(condition_rows[CONTROL_CONDITION]["task_id"])
        for condition_rows in pairs.values()
    }
    if len(task_ids) < MINIMUM_PAIRED_RUNS or len(task_ids) != len(pairs):
        raise ValueError(
            f"v2 evidence requires at least {MINIMUM_PAIRED_RUNS} unique task_ids, one per paired run"
        )

    return {
        "pairs": pairs,
        "pair_count": len(pairs),
        "row_count": len(rows),
    }


def raw_quality_score(row: Mapping[str, Any]) -> float:
    quality = row["quality"]
    assert isinstance(quality, Mapping)
    weighted = sum(float(quality[key]) * weight for key, weight in RUBRIC_WEIGHTS.items())
    return 50.0 * weighted


def adjusted_quality_score(row: Mapping[str, Any]) -> float:
    if row.get("outcome") != "passed" or row.get("resume_ready") is not True:
        return 0.0
    return raw_quality_score(row)


def _condition_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tokens = [int(row["tokensUsed"]) for row in rows]
    adjusted = [adjusted_quality_score(row) for row in rows]
    ready_count = sum(row["resume_ready"] is True for row in rows)
    failure_count = sum(row["outcome"] in {"failed", "blocked"} for row in rows)
    return {
        "row_count": len(rows),
        "tokensUsed_total": sum(tokens),
        "tokensUsed_mean": statistics.mean(tokens),
        "tokensUsed_median": statistics.median(tokens),
        "readiness_count": ready_count,
        "readiness_rate": ready_count / len(rows),
        "failure_count": failure_count,
        "failure_rate": failure_count / len(rows),
        "outcomes": {
            outcome: sum(row["outcome"] == outcome for row in rows)
            for outcome in sorted(OUTCOMES)
        },
        "adjusted_quality_mean": statistics.mean(adjusted),
    }


def _bytes_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    capsule = [int(row["capsule_bytes"]) for row in rows]
    prompt = [int(row["prompt_bytes"]) for row in rows]
    return {
        "capsule_total": sum(capsule),
        "capsule_mean": statistics.mean(capsule),
        "prompt_total": sum(prompt),
        "prompt_mean": statistics.mean(prompt),
    }


def build_v2_report(document: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_study_document(document)
    indexed_pairs = validated["pairs"]
    assert isinstance(indexed_pairs, dict)

    pair_reports: list[dict[str, Any]] = []
    savings: list[int] = []
    noninferior_failures: list[str] = []
    correctness_regressions: list[str] = []
    constraint_regressions: list[str] = []
    control_rows: list[Mapping[str, Any]] = []
    candidate_rows: list[Mapping[str, Any]] = []

    for paired_run_id in sorted(indexed_pairs):
        arms = indexed_pairs[paired_run_id]
        control = arms[CONTROL_CONDITION]
        candidate = arms[CANDIDATE_CONDITION]
        control_rows.append(control)
        candidate_rows.append(candidate)
        control_raw = raw_quality_score(control)
        candidate_raw = raw_quality_score(candidate)
        control_adjusted = adjusted_quality_score(control)
        candidate_adjusted = adjusted_quality_score(candidate)
        paired_savings = int(control["tokensUsed"]) - int(candidate["tokensUsed"])
        savings.append(paired_savings)

        if candidate_adjusted < control_adjusted:
            noninferior_failures.append(paired_run_id)
        if candidate["quality"]["task_correctness"] < control["quality"]["task_correctness"]:
            correctness_regressions.append(paired_run_id)
        if candidate["quality"]["constraint_adherence"] < control["quality"]["constraint_adherence"]:
            constraint_regressions.append(paired_run_id)

        pair_reports.append(
            {
                "paired_run_id": paired_run_id,
                "task_id": control["task_id"],
                "paired_savings_tokens": paired_savings,
                CONTROL_CONDITION: {
                    **control,
                    "raw_quality": control_raw,
                    "adjusted_quality": control_adjusted,
                },
                CANDIDATE_CONDITION: {
                    **candidate,
                    "raw_quality": candidate_raw,
                    "adjusted_quality": candidate_adjusted,
                },
            }
        )

    positive = sum(value > 0 for value in savings)
    negative = sum(value < 0 for value in savings)
    ties = sum(value == 0 for value in savings)
    median_savings = statistics.median(savings)
    sign_test_p = two_sided_sign_test_p_value(positive, negative)
    all_pairs_noninferior = not noninferior_failures
    no_correctness_regression = not correctness_regressions
    no_constraint_regression = not constraint_regressions
    stable_positive_direction = (
        sign_test_p < SIGN_TEST_ALPHA and positive > negative and median_savings > 0
    )
    quality_passed = (
        all_pairs_noninferior
        and no_correctness_regression
        and no_constraint_regression
    )
    candidate_all_passed_and_ready = all(
        row["outcome"] == "passed" and row["resume_ready"] is True
        for row in candidate_rows
    )
    empirical_gate_passed = (
        quality_passed
        and candidate_all_passed_and_ready
        and stable_positive_direction
    )

    return {
        "schema_version": V2_SCHEMA_VERSION,
        "study_type": V2_STUDY_TYPE,
        "qualifies_as_v2_evidence": True,
        "telemetry_scope": V2_TELEMETRY_SCOPE,
        "sample_size_pairs": validated["pair_count"],
        "row_count": validated["row_count"],
        "control_condition": CONTROL_CONDITION,
        "candidate_condition": CANDIDATE_CONDITION,
        "evidence_binding": {
            "control_commit_id": document["control_commit_id"],
            "candidate_commit_id": document["candidate_commit_id"],
            "control_runtime_sha256": document["control_runtime_sha256"],
            "candidate_runtime_sha256": document["candidate_runtime_sha256"],
            "preregistration": document["preregistration"],
        },
        "paired_savings_tokens": savings,
        "candidate_lower_token_pair_count": positive,
        "candidate_higher_token_pair_count": negative,
        "tied_pair_count": ties,
        "median_paired_savings_tokens": median_savings,
        "mean_paired_savings_tokens": statistics.mean(savings),
        "paired_sign_test_two_sided_p_value": sign_test_p,
        "sign_test_alpha": SIGN_TEST_ALPHA,
        "stable_positive_token_direction": stable_positive_direction,
        "quality_gate": {
            "all_pairs_noninferior": all_pairs_noninferior,
            "noninferior_failure_run_ids": noninferior_failures,
            "no_correctness_regression": no_correctness_regression,
            "correctness_regression_run_ids": correctness_regressions,
            "no_constraint_regression": no_constraint_regression,
            "constraint_regression_run_ids": constraint_regressions,
            "candidate_all_passed_and_ready": candidate_all_passed_and_ready,
        },
        "conditions": {
            CONTROL_CONDITION: _condition_summary(control_rows),
            CANDIDATE_CONDITION: _condition_summary(candidate_rows),
        },
        "bytes": {
            CONTROL_CONDITION: _bytes_summary(control_rows),
            CANDIDATE_CONDITION: _bytes_summary(candidate_rows),
        },
        "pairs": pair_reports,
        "empirical_gate_passed": empirical_gate_passed,
        "token_efficiency_claim_ready": empirical_gate_passed,
        "cost_claim_ready": False,
        "cost_warning": (
            "Exact goal-period tokensUsed does not split cached input, uncached input, "
            "and output; it cannot establish exact billing or cost savings."
        ),
        "evidence_verdict": (
            "passes_v2_token_efficiency_and_quality_gate"
            if empirical_gate_passed
            else "does_not_pass_v2_token_efficiency_and_quality_gate"
        ),
    }


def validate_v3_study_document(document: Mapping[str, Any]) -> dict[str, Any]:
    from goal_telemetry_v3 import validate_v3_study_document as validate

    return validate(document)


def build_v3_report(document: Mapping[str, Any]) -> dict[str, Any]:
    from goal_telemetry_v3 import build_v3_report as build

    return build(document)


def build_report(
    clean_tokens: Sequence[int],
    fork_tokens: Sequence[int],
    *,
    model: str,
    cached_input_rate: float,
    input_rate: float,
    output_rate: float,
) -> dict[str, Any]:
    """Build the historical clean/fork aggregate report.

    Token lists alone omit v2 readiness, outcome, byte, and rubric rows.
    """
    if len(clean_tokens) != len(fork_tokens):
        raise ValueError("clean and fork samples must have the same length")
    if not clean_tokens:
        raise ValueError("at least one paired trial is required")
    if any(rate < 0 for rate in (cached_input_rate, input_rate, output_rate)):
        raise ValueError("pricing rates must be non-negative")

    deltas = [fork - clean for clean, fork in zip(clean_tokens, fork_tokens)]
    clean_total = sum(clean_tokens)
    fork_total = sum(fork_tokens)
    mean_reduction = (statistics.mean(deltas) / statistics.mean(fork_tokens)) * 100
    savings_stdev = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
    clean_lower = sum(delta > 0 for delta in deltas)
    clean_higher = sum(delta < 0 for delta in deltas)
    ties = sum(delta == 0 for delta in deltas)
    sign_test_p = two_sided_sign_test_p_value(clean_lower, clean_higher)
    median_savings = statistics.median(deltas)

    if sign_test_p < 0.05 and median_savings > 0:
        efficiency_verdict = "evidence_clean_uses_fewer_goal_tokens"
    elif sign_test_p < 0.05 and median_savings < 0:
        efficiency_verdict = "evidence_clean_uses_more_goal_tokens"
    else:
        efficiency_verdict = "no_statistically_stable_goal_token_improvement"

    scenarios = {}
    for name, rate in (
        ("all_cached_input", cached_input_rate),
        ("all_uncached_input", input_rate),
        ("all_output", output_rate),
    ):
        clean_cost = token_cost(clean_total, rate)
        fork_cost = token_cost(fork_total, rate)
        scenarios[name] = {
            "rate_per_million_tokens_usd": rate,
            "clean_total_usd": clean_cost,
            "fork_total_usd": fork_cost,
            "savings_usd": fork_cost - clean_cost,
            "median_paired_savings_usd": token_cost(median_savings, rate),
        }

    return {
        "evidence_schema": "legacy_clean_fork_aggregate_v1",
        "qualifies_as_v2_evidence": False,
        "telemetry_scope": "goal_tokens_used_not_full_context_or_billing_usage",
        "model": model,
        "sample_size": len(clean_tokens),
        "clean_tokens": list(clean_tokens),
        "fork_tokens": list(fork_tokens),
        "paired_savings_tokens": deltas,
        "clean_lower_pair_count": clean_lower,
        "clean_higher_pair_count": clean_higher,
        "tied_pair_count": ties,
        "clean_mean_tokens": statistics.mean(clean_tokens),
        "fork_mean_tokens": statistics.mean(fork_tokens),
        "clean_median_tokens": statistics.median(clean_tokens),
        "fork_median_tokens": statistics.median(fork_tokens),
        "mean_paired_savings_tokens": statistics.mean(deltas),
        "median_paired_savings_tokens": median_savings,
        "paired_savings_sample_stdev_tokens": savings_stdev,
        "paired_savings_standard_error_tokens": savings_stdev / math.sqrt(len(deltas)),
        "paired_sign_test_two_sided_p_value": sign_test_p,
        "efficiency_verdict": efficiency_verdict,
        "mean_token_reduction_percent": mean_reduction,
        "clean_total_tokens": clean_total,
        "fork_total_tokens": fork_total,
        "cost_sensitivity": scenarios,
        "cost_warning": (
            "Goal tokens do not split cached input, uncached input, and output. "
            "Scenario costs are sensitivity bounds, not exact Codex charges."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate V2/V3 paired telemetry or reproduce the legacy aggregate report."
    )
    parser.add_argument("--study-json", type=Path)
    parser.add_argument("--clean-tokens", type=parse_token_list)
    parser.add_argument("--fork-tokens", type=parse_token_list)
    parser.add_argument("--model")
    parser.add_argument("--cached-input-rate", type=float)
    parser.add_argument("--input-rate", type=float)
    parser.add_argument("--output-rate", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.study_json is not None:
            if any(
                value is not None
                for value in (
                    args.clean_tokens,
                    args.fork_tokens,
                    args.model,
                    args.cached_input_rate,
                    args.input_rate,
                    args.output_rate,
                )
            ):
                raise ValueError("--study-json cannot be combined with legacy aggregate inputs")
            document = json.loads(args.study_json.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("--study-json must contain a JSON object")
            study_identity = (
                document.get("schema_version"),
                document.get("study_type"),
            )
            if study_identity == (V2_SCHEMA_VERSION, V2_STUDY_TYPE):
                report: Mapping[str, Any] = build_v2_report(document)
            elif study_identity == (V3_SCHEMA_VERSION, V3_STUDY_TYPE):
                report = build_v3_report(document)
            else:
                raise ValueError(
                    "unsupported study schema/type identity; mixed or partial markers are invalid"
                )
        else:
            required = {
                "--clean-tokens": args.clean_tokens,
                "--fork-tokens": args.fork_tokens,
                "--model": args.model,
                "--cached-input-rate": args.cached_input_rate,
                "--input-rate": args.input_rate,
                "--output-rate": args.output_rate,
            }
            missing = [flag for flag, value in required.items() if value is None]
            if missing:
                raise ValueError(
                    "legacy aggregate mode requires " + ", ".join(missing)
                )
            report = build_report(
                args.clean_tokens,
                args.fork_tokens,
                model=args.model,
                cached_input_rate=args.cached_input_rate,
                input_rate=args.input_rate,
                output_rate=args.output_rate,
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
