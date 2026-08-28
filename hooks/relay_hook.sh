#!/usr/bin/env bash
# Canonical plugin hook entry for Relay.
set -euo pipefail

ROOT="${ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
PLUGIN_ROOT="${PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
EVENT="${1:-UserPromptSubmit}"
SCRIPT="$PLUGIN_ROOT/skills/relay/scripts/relay.py"
PAYLOAD="$(cat || true)"

case "$EVENT" in
  PreCompact|pre-compact)
    OFFICIAL_EVENT="PreCompact"
    ;;
  UserPromptSubmit|user-prompt-submit)
    OFFICIAL_EVENT="UserPromptSubmit"
    ;;
  PreToolUse|pre-tool-use)
    OFFICIAL_EVENT="PreToolUse"
    ;;
  SessionEnd|session-end)
    OFFICIAL_EVENT="SessionEnd"
    ;;
  *)
    printf '%s\n' '{"continue":true}'
    exit 0
    ;;
esac

allow_response() {
  if [[ "$OFFICIAL_EVENT" == "PreToolUse" || "$OFFICIAL_EVENT" == "SessionEnd" ]]; then
    printf '%s\n' '{}'
  elif [[ "$OFFICIAL_EVENT" == "PreCompact" && "${1:-}" == "python" ]]; then
    printf '%s\n' '{"continue":true,"systemMessage":"Relay requires Python 3.10 or newer. Native Codex compaction will continue."}'
  elif [[ "$OFFICIAL_EVENT" == "PreCompact" && "${1:-}" == "execution" ]]; then
    printf '%s\n' '{"continue":true,"systemMessage":"Relay could not start; native Codex compaction will continue."}'
  else
    printf '%s\n' '{"continue":true}'
  fi
}

if [[ ! -f "$SCRIPT" ]]; then
  allow_response
  exit 0
fi

if [[ -n "${RELAY_PYTHON:-}" ]]; then
  PYTHON_CANDIDATES=("$RELAY_PYTHON")
else
  PYTHON_CANDIDATES=(
    python3
    python3.10
    python3.11
    python3.12
    python3.13
    python3.14
    python3.15
  )
fi

PYTHON_BIN=""
PYTHON_VERSION=""
for candidate in "${PYTHON_CANDIDATES[@]}"; do
  executable="$(command -v "$candidate" 2>/dev/null || true)"
  [[ -n "$executable" ]] || continue
  version="$("$executable" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)"
  if [[ "$version" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    if (( major == 3 && minor >= 10 )); then
      PYTHON_BIN="$executable"
      PYTHON_VERSION="$version"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -n "${RELAY_PYTHON:-}" ]]; then
    printf '%s\n' "Relay requires Python 3.10 or newer; RELAY_PYTHON=$RELAY_PYTHON is missing, unusable, or too old. Install Python 3.10+ or point RELAY_PYTHON at a supported executable. Relay is failing open to native Codex behavior." >&2
  else
    printf '%s\n' 'Relay requires Python 3.10 or newer. Install Python 3.10+ and make it available as python3, or set RELAY_PYTHON to a supported executable. Relay is failing open to native Codex behavior.' >&2
  fi
  allow_response python
  exit 0
fi

set +e
OUTPUT="$("$PYTHON_BIN" "$SCRIPT" \
  --repo "$ROOT" \
  --stdin-json \
  --official-hook-event "$OFFICIAL_EVENT" \
  <<<"$PAYLOAD" 2>/dev/null)"
STATUS=$?
set -e

if [[ $STATUS -eq 0 && "$OUTPUT" == \{* ]]; then
  printf '%s\n' "$OUTPUT"
else
  printf '%s\n' "Relay hook could not run with Python $PYTHON_VERSION (exit status $STATUS); continuing with native Codex behavior." >&2
  allow_response execution
fi
