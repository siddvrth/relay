"""Read the context usage signals emitted by current Codex builds.

The app-server notification is the preferred source.  Hook payloads normally
only expose ``transcript_path``, so the exact current ``event_msg`` token-count
record is the compatibility fallback.  Unknown shapes return ``None`` and
the caller fails open.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypeAlias


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
# The Codex TUI reserves this baseline before displaying context percentage.
BASELINE_TOKENS: Final = 12_000


def _ratio(tokens: object, window: object) -> float | None:
    if (
        isinstance(tokens, bool)
        or not isinstance(tokens, (int, float))
        or isinstance(window, bool)
        or not isinstance(window, (int, float))
        or not math.isfinite(float(tokens))
        or not math.isfinite(float(window))
        or float(tokens) < 0
        or float(window) <= BASELINE_TOKENS
    ):
        return None
    effective_window = float(window) - BASELINE_TOKENS
    used = max(0.0, float(tokens) - BASELINE_TOKENS)
    return max(0.0, min(1.0, used / effective_window))


def parse_ratio(value: JsonValue) -> float | None:
    """Parse an explicit test/diagnostic ratio without accepting field aliases."""

    if value is None or isinstance(value, (bool, dict, list)):
        return None
    try:
        text = str(value).strip()
        if text.endswith("%"):
            parsed = float(text[:-1].strip()) / 100.0
        else:
            parsed = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    if parsed > 1:
        parsed /= 100.0
    return parsed if 0 <= parsed <= 1 else None


def _app_server_notification(payload: Mapping[str, JsonValue]) -> float | None:
    if payload.get("method") != "thread/tokenUsage/updated":
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    usage = params.get("tokenUsage")
    if not isinstance(usage, dict):
        return None
    latest = usage.get("last")
    if not isinstance(latest, dict):
        return None
    return _ratio(latest.get("totalTokens"), usage.get("modelContextWindow"))


def _transcript_record(record: Mapping[str, JsonValue]) -> float | None:
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    latest = info.get("last_token_usage")
    if not isinstance(latest, dict):
        return None
    # Current Codex uses latest total_tokens for the active context size.
    return _ratio(latest.get("total_tokens"), info.get("model_context_window"))


def _transcript_context_used(path_value: object) -> float | None:
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
            decoded = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, dict):
            continue
        if decoded.get("type") != "event_msg":
            continue
        payload = decoded.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        # If the newest token-count record has an unknown schema, do not use a
        # stale older value.  This is the fail-open boundary for Codex changes.
        return _transcript_record(decoded)
    return None


def extract_context_used(payload: Mapping[str, JsonValue]) -> float | None:
    """Return current context occupancy as a ratio, or ``None`` if unknown."""

    direct = _app_server_notification(payload)
    if direct is not None:
        return direct
    return _transcript_context_used(payload.get("transcript_path"))
