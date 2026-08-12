#!/usr/bin/env bash
# Canonical plugin hook entry for Relay.
set -euo pipefail

ROOT="${ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
PLUGIN_ROOT="${PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
EVENT="${1:-UserPromptSubmit}"
SCRIPT="$PLUGIN_ROOT/skills/relay/scripts/relay.py"
THRESHOLD="${RELAY_THRESHOLD:-0.30}"
PAYLOAD="$(cat || true)"

case "$EVENT" in
  UserPromptSubmit|user-prompt-submit)
    OFFICIAL_EVENT="UserPromptSubmit"
    ;;
  PreToolUse|pre-tool-use)
    OFFICIAL_EVENT="PreToolUse"
    ;;
  *)
    printf '%s\n' '{"continue":true}'
    exit 0
    ;;
esac

if [[ ! -f "$SCRIPT" ]] || ! command -v python3 >/dev/null 2>&1; then
  if [[ "$OFFICIAL_EVENT" == "PreToolUse" ]]; then
    printf '%s\n' '{}'
  else
    printf '%s\n' '{"continue":true}'
  fi
  exit 0
fi

set +e
OUTPUT="$(python3 "$SCRIPT" \
  --repo "$ROOT" \
  --stdin-json \
  --handoff-threshold "$THRESHOLD" \
  --official-hook-event "$OFFICIAL_EVENT" \
  <<<"$PAYLOAD" 2>/dev/null)"
STATUS=$?
set -e

if [[ $STATUS -eq 0 && "$OUTPUT" == \{* ]]; then
  printf '%s\n' "$OUTPUT"
elif [[ "$OFFICIAL_EVENT" == "PreToolUse" ]]; then
  printf '%s\n' '{}'
else
  printf '%s\n' '{"continue":true}'
fi
