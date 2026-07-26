from __future__ import annotations

import statistics
from typing import Any, Mapping, Sequence

import goal_telemetry_report as telemetry
from goal_telemetry_v3_contract import (
    CONTINUATION_QUALITY_KEYS,
    TOKEN_COMPONENT_KEYS,
    V3_SCHEMA_VERSION,
    V3_STUDY_TYPE,
    V3_TELEMETRY_SCOPE,
)


def _distribution(values: Sequence[Any]) -> dict[str, int]:
    return {str(value): values.count(value) for value in sorted(set(values), key=str)}


def _latency_summary(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values = [float(row[key]) for row in rows if row[key] is not None]
    if not values:
        return {"count": 0, "minimum": None, "mean": None, "median": None, "maximum": None}
    return {
        "count": len(values),
        "minimum": min(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def _integer_summary(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    values = [int(row[key]) for row in rows]
    return {
        "count": len(values),
        "total": sum(values),
        "minimum": min(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def _continuation_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        key: {
            "passed_count": sum(bool(row["continuation_quality"][key]) for row in rows),
            "pass_rate": (
                sum(bool(row["continuation_quality"][key]) for row in rows) / len(rows)
                if rows
                else 0.0
            ),
        }
        for key in sorted(CONTINUATION_QUALITY_KEYS)
    }


def _observability_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pending_observed = sum(bool(row["termination_pending_observed"]) for row in rows)
    pending_recovered = sum(bool(row["termination_pending_recovered"]) for row in rows)
    return {
        "handoff_count_total": sum(int(row["handoff_count"]) for row in rows),
        "duplicated_work_action_count_total": sum(
            int(row["duplicated_work_action_count"]) for row in rows
        ),
        "post_ack_source_tokens_total": sum(int(row["post_ack_source_tokens"]) for row in rows),
        "post_ack_source_actions_total": sum(int(row["post_ack_source_actions"]) for row in rows),
        "transfer_latency_ms": _latency_summary(rows, "transfer_latency_ms"),
        "acknowledgement_outcomes": _distribution(
            [row["acknowledgement_outcome"] for row in rows]
        ),
        "acknowledgement_latency_ms": _latency_summary(rows, "acknowledgement_latency_ms"),
        "acknowledgement_attempt_count_total": sum(
            int(row["acknowledgement_attempt_count"]) for row in rows
        ),
        "acknowledgement_failure_count_total": sum(
            int(row["acknowledgement_failure_count"]) for row in rows
        ),
        "source_stop_capabilities": _distribution(
            [row["source_stop_capability"] for row in rows]
        ),
        "source_stop_outcomes": _distribution([row["source_stop_outcome"] for row in rows]),
        "source_stop_latency_ms": _latency_summary(rows, "source_stop_latency_ms"),
        "source_stop_attempt_count_total": sum(
            int(row["source_stop_attempt_count"]) for row in rows
        ),
        "source_stop_failure_count_total": sum(
            int(row["source_stop_failure_count"]) for row in rows
        ),
        "duplicate_destination_count_total": sum(
            int(row["duplicate_destination_count"]) for row in rows
        ),
        "ownership_conflict_count_total": sum(
            int(row["ownership_conflict_count"]) for row in rows
        ),
        "retry_count_total": sum(int(row["retry_count"]) for row in rows),
        "retry_count_distribution": _distribution([row["retry_count"] for row in rows]),
        "termination_pending_observed_count": pending_observed,
        "termination_pending_recovered_count": pending_recovered,
        "termination_pending_recovery_rate": (
            pending_recovered / pending_observed if pending_observed else None
        ),
        "human_intervention_count": sum(
            int(row["human_intervention_count"]) for row in rows
        ),
    }


def _chain_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        f"{key}_total": sum(int(row[key]) for row in rows)
        for key in TOKEN_COMPONENT_KEYS
    }


def build_v3_report(
    document: Mapping[str, Any], validated: Mapping[str, Any]
) -> dict[str, Any]:
    claim_rows = validated["claim_rows"]
    diagnostic_rows = validated["diagnostic_rows"]
    assert isinstance(claim_rows, list)
    assert isinstance(diagnostic_rows, list)
    base = telemetry.build_v2_report(validated["projection"])
    by_pair = {
        (str(row["paired_run_id"]), str(row["condition"])): row for row in claim_rows
    }
    pairs: list[dict[str, Any]] = []
    for pair in base["pairs"]:
        enriched = dict(pair)
        for condition in (telemetry.CONTROL_CONDITION, telemetry.CANDIDATE_CONDITION):
            original = dict(by_pair[(str(pair["paired_run_id"]), condition)])
            original["raw_quality"] = pair[condition]["raw_quality"]
            original["adjusted_quality"] = pair[condition]["adjusted_quality"]
            enriched[condition] = original
        pairs.append(enriched)
    conditions: dict[str, Any] = {}
    for condition in (telemetry.CONTROL_CONDITION, telemetry.CANDIDATE_CONDITION):
        rows = [row for row in claim_rows if row["condition"] == condition]
        conditions[condition] = {
            **base["conditions"][condition],
            "chain_tokens": _chain_summary(rows),
            "observability": _observability_summary(rows),
            "continuation_quality": _continuation_summary(rows),
        }
    byte_distributions = {
        condition: {
            "capsule_bytes": _integer_summary(
                [row for row in claim_rows if row["condition"] == condition], "capsule_bytes"
            ),
            "prompt_bytes": _integer_summary(
                [row for row in claim_rows if row["condition"] == condition], "prompt_bytes"
            ),
        }
        for condition in (telemetry.CONTROL_CONDITION, telemetry.CANDIDATE_CONDITION)
    }
    diagnostic_conditions: dict[str, Any] = {}
    for condition in sorted({str(row["condition"]) for row in diagnostic_rows}):
        rows = [row for row in diagnostic_rows if row["condition"] == condition]
        diagnostic_conditions[condition] = {
            "qualifies_as_claim_evidence": False,
            "row_count": len(rows),
            "tokensUsed_total": sum(int(row["tokensUsed"]) for row in rows),
            "chain_tokens": _chain_summary(rows),
            "observability": _observability_summary(rows),
            "continuation_quality": _continuation_summary(rows),
        }
    candidate_rows = [
        row for row in claim_rows if row["condition"] == telemetry.CANDIDATE_CONDITION
    ]
    zero_post_ack = all(
        row["post_ack_source_tokens"] == 0 and row["post_ack_source_actions"] == 0
        for row in candidate_rows
    )
    continuation_passed = all(
        all(bool(value) for value in row["continuation_quality"].values())
        for row in candidate_rows
    )
    all_acknowledged = all(
        row["acknowledgement_outcome"] == "accepted" for row in candidate_rows
    )
    all_stops_observed = all(
        row["source_stop_outcome"] in {"interrupted", "quiesced", "already_exited"}
        for row in candidate_rows
    )
    no_unresolved_pending = all(
        not row["termination_pending_observed"] or row["termination_pending_recovered"]
        for row in candidate_rows
    )
    no_intervention = all(row["human_intervention_count"] == 0 for row in candidate_rows)
    v3_gate = all(
        (
            bool(base["empirical_gate_passed"]),
            zero_post_ack,
            continuation_passed,
            all_acknowledged,
            all_stops_observed,
            no_unresolved_pending,
            no_intervention,
        )
    )
    quality_gate = {
        **base["quality_gate"],
        "candidate_zero_post_ack_source_activity": zero_post_ack,
        "candidate_all_continuation_quality_checks_passed": continuation_passed,
        "candidate_all_acknowledged": all_acknowledged,
        "candidate_all_source_stops_observed": all_stops_observed,
        "candidate_no_unresolved_termination_pending": no_unresolved_pending,
        "candidate_no_human_intervention": no_intervention,
    }
    return {
        **base,
        "schema_version": V3_SCHEMA_VERSION,
        "study_type": V3_STUDY_TYPE,
        "telemetry_scope": V3_TELEMETRY_SCOPE,
        "qualifies_as_v2_evidence": False,
        "qualifies_as_v3_evidence": True,
        "claim_row_count": validated["claim_row_count"],
        "row_count": validated["row_count"],
        "conditions": conditions,
        "bytes": byte_distributions,
        "diagnostics": {
            "qualifies_as_claim_evidence": False,
            "row_count": len(diagnostic_rows),
            "conditions": diagnostic_conditions,
        },
        "pairs": pairs,
        "quality_gate": quality_gate,
        "empirical_gate_passed": v3_gate,
        "token_efficiency_claim_ready": v3_gate,
        "cost_claim_ready": False,
        "evidence_verdict": (
            "passes_v3_acknowledgement_gated_token_efficiency_and_quality_gate"
            if v3_gate
            else "does_not_pass_v3_acknowledgement_gated_token_efficiency_and_quality_gate"
        ),
    }
