#!/usr/bin/env python3
from __future__ import annotations

from typing import Any, Mapping

import goal_telemetry_report as telemetry
from goal_telemetry_v3_contract import (
    V3_SCHEMA_VERSION,
    V3_STUDY_TYPE,
    V3_TELEMETRY_SCOPE,
)
from goal_telemetry_v3_schema import v2_projection, validate_v3_row


def validate_v3_study_document(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise ValueError("v3 study document must be an object")
    telemetry._require_exact_keys(document, telemetry.DOCUMENT_KEYS, "v3 study document")
    if document.get("schema_version") != V3_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {V3_SCHEMA_VERSION}")
    if document.get("study_type") != V3_STUDY_TYPE:
        raise ValueError(f"study_type must be {V3_STUDY_TYPE}")
    if document.get("telemetry_scope") != V3_TELEMETRY_SCOPE:
        raise ValueError(f"telemetry_scope must be {V3_TELEMETRY_SCOPE}")
    preregistration = document.get("preregistration")
    if not isinstance(preregistration, Mapping):
        raise ValueError("preregistration must be an object")
    frozen_at = telemetry._parse_offset_timestamp(preregistration.get("frozen_at"))
    if frozen_at is None:
        raise ValueError("preregistration frozen_at must be an offset-aware ISO-8601 timestamp")
    rows = document.get("rows")
    if not isinstance(rows, list):
        raise ValueError("rows must be a list")
    claim_rows: list[Mapping[str, Any]] = []
    diagnostic_rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"row {index} must be an object")
        target = claim_rows if validate_v3_row(row, index, frozen_at) else diagnostic_rows
        target.append(row)
    projection = v2_projection(document, claim_rows)
    v2_validated = telemetry.validate_study_document(projection)
    return {
        "claim_rows": claim_rows,
        "diagnostic_rows": diagnostic_rows,
        "projection": projection,
        "pair_count": v2_validated["pair_count"],
        "claim_row_count": len(claim_rows),
        "row_count": len(rows),
    }


def build_v3_report(document: Mapping[str, Any]) -> dict[str, Any]:
    from goal_telemetry_v3_report import build_v3_report as build

    return build(document, validate_v3_study_document(document))
