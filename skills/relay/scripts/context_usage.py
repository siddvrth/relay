from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Final, Mapping, TypeAlias


JsonValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)
TAIL_BYTES: Final = 256 * 1024
PERCENT_FIELD_NAMES: Final = (
    "context_usage_percent",
    "contextUsagePercent",
    "context_used_percent",
    "contextUsedPercent",
    "tokenUsagePercent",
    "token_usage_percent",
    "usagePercent",
    "usage_percent",
)
RATIO_FIELD_NAMES: Final = (
    "contextUsed",
    "context_used",
    "contextUsage",
    "context_usage",
    "contextUsedRatio",
    "context_used_ratio",
    "usageRatio",
    "usage_ratio",
)


def _finite_ratio(value: float) -> float | None:
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return value


def parse_ratio(value: JsonValue) -> float | None:
    if value is None or isinstance(value, bool | dict | list):
        return None
    try:
        text = str(value).strip().lower()
        if not text or text in {"unknown", "n/a", "na"}:
            return None
        if text.endswith("%"):
            parsed = float(text[:-1].strip()) / 100
        else:
            parsed = float(text)
            parsed = parsed / 100 if parsed > 1 else parsed
    except (TypeError, ValueError, OverflowError):
        return None
    return _finite_ratio(parsed)


def parse_percent(value: JsonValue) -> float | None:
    if value is None or isinstance(value, bool | dict | list):
        return None
    try:
        text = str(value).strip().lower()
        if text.endswith("%"):
            text = text[:-1].strip()
        parsed = float(text) / 100
    except (TypeError, ValueError, OverflowError):
        return None
    return _finite_ratio(parsed)


def _compatible_context_used(payload: Mapping[str, JsonValue]) -> float | None:
    for key in PERCENT_FIELD_NAMES:
        if key in payload:
            ratio = parse_percent(payload[key])
            if ratio is not None:
                return ratio

    for key in RATIO_FIELD_NAMES:
        if key in payload:
            ratio = parse_ratio(payload[key])
            if ratio is not None:
                return ratio

    tokens = payload.get("context_tokens")
    window = payload.get("context_window_size")
    if tokens is not None and window is not None:
        try:
            tokens_value = float(tokens)
            window_value = float(window)
        except (TypeError, ValueError, OverflowError):
            return None
        if (
            math.isfinite(tokens_value)
            and math.isfinite(window_value)
            and tokens_value >= 0
            and window_value > 0
        ):
            ratio = _finite_ratio(tokens_value / window_value)
            if ratio is not None:
                return ratio

    for container_key in ("context", "telemetry", "usage", "metrics"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            ratio = _compatible_context_used(container)
            if ratio is not None:
                return ratio
    return None


def _token_count_ratio(record: Mapping[str, JsonValue]) -> float | None:
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    latest = info.get("last_token_usage")
    if not isinstance(latest, dict):
        return None
    input_tokens = latest.get("input_tokens")
    model_window = info.get("model_context_window")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int | float)
        or isinstance(model_window, bool)
        or not isinstance(model_window, int | float)
        or input_tokens < 0
        or model_window <= 0
    ):
        return None
    return _finite_ratio(float(input_tokens) / float(model_window))


def _transcript_context_used(path_value: JsonValue) -> float | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    path = Path(path_value).expanduser()
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            tail = handle.read(TAIL_BYTES)
    except OSError:
        return None
    if start:
        separator = tail.find(b"\n")
        if separator < 0:
            return None
        tail = tail[separator + 1 :]
    lines = tail.splitlines()
    if tail and not tail.endswith(b"\n") and lines:
        lines.pop()
    for encoded in reversed(lines):
        try:
            decoded: JsonValue = json.loads(encoded)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(decoded, dict) or decoded.get("type") != "event_msg":
            continue
        payload = decoded.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        return _token_count_ratio(decoded)
    return None


def extract_context_used(payload: Mapping[str, JsonValue]) -> float | None:
    compatible = _compatible_context_used(payload)
    if compatible is not None:
        return compatible
    return _transcript_context_used(payload.get("transcript_path"))
