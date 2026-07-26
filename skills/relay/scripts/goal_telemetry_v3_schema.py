from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping, Sequence

import goal_telemetry_report as telemetry
from goal_telemetry_v3_contract import (
    ACKNOWLEDGEMENT_OUTCOMES,
    CONTINUATION_QUALITY_KEYS,
    COUNT_KEYS,
    LATENCY_KEYS,
    SOURCE_STOP_CAPABILITIES,
    SOURCE_STOP_OUTCOMES,
    STOP_COMPATIBILITY,
    TOKEN_COMPONENT_KEYS,
    V3_ROW_KEYS,
)


def _finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def _validate_base_row(row: Mapping[str, Any], index: int, frozen_at: dt.datetime) -> None:
    for key in ("paired_run_id", "task_id", "condition", "rubric_notes"):
        if not telemetry._nonempty_string(row.get(key)):
            raise ValueError(f"row {index} {key} must be a non-empty string")
    run_started_at = telemetry._parse_offset_timestamp(row.get("run_started_at"))
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
    if not telemetry._nonnegative_int(row.get("tokensUsed")):
        raise ValueError(f"row {index} tokensUsed must be a non-negative integer")
    if row.get("outcome") not in telemetry.OUTCOMES:
        raise ValueError(f"row {index} outcome must be passed, failed, or blocked")
    if not isinstance(row.get("resume_ready"), bool):
        raise ValueError(f"row {index} resume_ready must be boolean")
    for key in ("capsule_bytes", "prompt_bytes"):
        if not telemetry._nonnegative_int(row.get(key)):
            raise ValueError(f"row {index} {key} must be a non-negative integer")
    quality = row.get("quality")
    if not isinstance(quality, Mapping):
        raise ValueError(f"row {index} quality must be an object")
    telemetry._require_exact_keys(quality, set(telemetry.RUBRIC_WEIGHTS), f"row {index} quality")
    if any(
        not isinstance(score, int)
        or isinstance(score, bool)
        or not 0 <= score <= 2
        for score in quality.values()
    ):
        raise ValueError("quality scores must be integers from 0 to 2")


def v2_projection(
    document: Mapping[str, Any], claim_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    projected = {key: document[key] for key in telemetry.DOCUMENT_KEYS}
    projected.update(
        {
            "schema_version": telemetry.V2_SCHEMA_VERSION,
            "study_type": telemetry.V2_STUDY_TYPE,
            "telemetry_scope": telemetry.V2_TELEMETRY_SCOPE,
            "rows": [{key: row[key] for key in telemetry.ROW_KEYS} for row in claim_rows],
        }
    )
    return projected


def validate_v3_row(row: Mapping[str, Any], index: int, frozen_at: dt.datetime) -> bool:
    telemetry._require_exact_keys(row, V3_ROW_KEYS, "v3 row")
    _validate_base_row(row, index, frozen_at)
    qualifies = row.get("qualifies_as_claim_evidence")
    if not isinstance(qualifies, bool):
        raise ValueError(f"row {index} qualifies_as_claim_evidence must be boolean")
    is_claim = row.get("condition") in {
        telemetry.CONTROL_CONDITION,
        telemetry.CANDIDATE_CONDITION,
    }
    if is_claim and not qualifies:
        raise ValueError("claim conditions must qualify as claim evidence")
    if not is_claim and qualifies:
        raise ValueError("diagnostic conditions cannot qualify as claim evidence")
    for key in (*TOKEN_COMPONENT_KEYS, *COUNT_KEYS):
        if not telemetry._nonnegative_int(row.get(key)):
            raise ValueError(f"row {index} {key} must be a non-negative integer")
    if int(row["tokensUsed"]) != sum(int(row[key]) for key in TOKEN_COMPONENT_KEYS):
        raise ValueError("tokensUsed must equal four chain components")
    for key in LATENCY_KEYS:
        value = row.get(key)
        if value is not None and not _finite_nonnegative_number(value):
            raise ValueError(f"row {index} {key} must be a finite non-negative number")
    if row.get("acknowledgement_outcome") not in ACKNOWLEDGEMENT_OUTCOMES:
        raise ValueError(f"row {index} acknowledgement_outcome is invalid")
    capability = row.get("source_stop_capability")
    stop_outcome = row.get("source_stop_outcome")
    if capability not in SOURCE_STOP_CAPABILITIES:
        raise ValueError(f"row {index} source_stop_capability is invalid")
    if stop_outcome not in SOURCE_STOP_OUTCOMES:
        raise ValueError(f"row {index} source_stop_outcome is invalid")
    if stop_outcome not in STOP_COMPATIBILITY[str(capability)]:
        raise ValueError("incompatible source stop capability and outcome")
    for key in ("termination_pending_observed", "termination_pending_recovered"):
        if not isinstance(row.get(key), bool):
            raise ValueError(f"row {index} {key} must be boolean")
    if row["termination_pending_recovered"] and not row["termination_pending_observed"]:
        raise ValueError("recovery requires termination pending observation")
    if stop_outcome == "termination_pending" and not row["termination_pending_observed"]:
        raise ValueError("termination_pending outcome requires pending observation")
    if row["termination_pending_recovered"] and stop_outcome == "termination_pending":
        raise ValueError("recovered termination pending requires a final stop outcome")
    if row["acknowledgement_failure_count"] > row["acknowledgement_attempt_count"]:
        raise ValueError("failure count cannot exceed attempt count")
    if row["source_stop_failure_count"] > row["source_stop_attempt_count"]:
        raise ValueError("failure count cannot exceed attempt count")
    continuation = row.get("continuation_quality")
    if not isinstance(continuation, Mapping):
        raise ValueError(f"row {index} continuation_quality must be an object")
    telemetry._require_exact_keys(continuation, CONTINUATION_QUALITY_KEYS, "continuation_quality")
    if any(not isinstance(value, bool) for value in continuation.values()):
        raise ValueError("continuation_quality values must be boolean")
    if row["handoff_count"] == 0:
        if (
            row["acknowledgement_outcome"] != "not_applicable"
            or capability != "not_applicable"
            or stop_outcome != "not_applicable"
        ):
            raise ValueError("zero-handoff outcomes must be not_applicable")
        if any(row[key] is not None for key in LATENCY_KEYS):
            raise ValueError("zero-handoff latencies must be null")
        zero_counts = (
            "acknowledgement_attempt_count",
            "acknowledgement_failure_count",
            "source_stop_attempt_count",
            "source_stop_failure_count",
            "retry_count",
            "post_ack_source_tokens",
            "post_ack_source_actions",
        )
        if any(row[key] != 0 for key in zero_counts):
            raise ValueError("zero-handoff lifecycle counts must be zero")
        if row["termination_pending_observed"] or row["termination_pending_recovered"]:
            raise ValueError("zero-handoff rows cannot observe termination pending")
    else:
        if (
            row["acknowledgement_outcome"] == "not_applicable"
            or capability == "not_applicable"
            or stop_outcome == "not_applicable"
        ):
            raise ValueError("handoff outcomes must be applicable")
        if any(row[key] is None for key in LATENCY_KEYS):
            raise ValueError("handoff latencies must be finite non-negative numbers")
        if row["acknowledgement_attempt_count"] == 0 or row["source_stop_attempt_count"] == 0:
            raise ValueError("handoff acknowledgement and stop attempts must be positive")
    if (
        row.get("condition") == telemetry.CANDIDATE_CONDITION
        and row.get("outcome") == "passed"
        and row.get("resume_ready") is True
        and (row["post_ack_source_tokens"] or row["post_ack_source_actions"])
    ):
        raise ValueError("passing candidate post-ack source activity must be zero")
    return is_claim
