from __future__ import annotations

import goal_telemetry_report as telemetry

V3_SCHEMA_VERSION = 3
V3_STUDY_TYPE = "token_efficient_relay_v3"
V3_TELEMETRY_SCOPE = "aggregate_source_destination_chain_tokensUsed"
CONTINUATION_QUALITY_KEYS = {
    "next_step_correct",
    "constraints_retained",
    "no_completed_work_repeated",
    "no_remaining_work_skipped",
    "repository_goal_reconciled",
    "validation_sufficient",
    "middle_critical_fact_recovered",
}
TOKEN_COMPONENT_KEYS = (
    "source_tokens_before_handoff",
    "handoff_generation_tokens",
    "destination_resume_tokens",
    "completion_tokens_after_resume",
)
COUNT_KEYS = (
    "handoff_count",
    "duplicated_work_action_count",
    "post_ack_source_tokens",
    "post_ack_source_actions",
    "acknowledgement_attempt_count",
    "acknowledgement_failure_count",
    "source_stop_attempt_count",
    "source_stop_failure_count",
    "duplicate_destination_count",
    "ownership_conflict_count",
    "retry_count",
    "human_intervention_count",
)
LATENCY_KEYS = (
    "transfer_latency_ms",
    "acknowledgement_latency_ms",
    "source_stop_latency_ms",
)
ACKNOWLEDGEMENT_OUTCOMES = {
    "accepted",
    "timed_out",
    "stale_rejected",
    "replay_rejected",
    "cross_session_rejected",
    "failed",
    "not_applicable",
}
SOURCE_STOP_CAPABILITIES = {
    "native_interrupt",
    "cooperative",
    "hook_read_only",
    "process_group_interruption_unavailable",
    "unsupported",
    "not_applicable",
}
SOURCE_STOP_OUTCOMES = {
    "interrupted",
    "quiesced",
    "already_exited",
    "unsupported",
    "failed",
    "termination_pending",
    "not_applicable",
}
STOP_COMPATIBILITY = {
    "native_interrupt": {"interrupted", "already_exited", "failed", "termination_pending"},
    "cooperative": {"quiesced", "already_exited", "failed", "termination_pending"},
    "hook_read_only": {"unsupported", "failed", "termination_pending"},
    "process_group_interruption_unavailable": {"unsupported", "termination_pending"},
    "unsupported": {"unsupported", "termination_pending"},
    "not_applicable": {"not_applicable"},
}
V3_ROW_KEYS = telemetry.ROW_KEYS | {
    "qualifies_as_claim_evidence",
    *TOKEN_COMPONENT_KEYS,
    *COUNT_KEYS,
    *LATENCY_KEYS,
    "acknowledgement_outcome",
    "source_stop_capability",
    "source_stop_outcome",
    "termination_pending_observed",
    "termination_pending_recovered",
    "continuation_quality",
}
