#!/usr/bin/env bash
# Repo-local OMX / Codex hook entry for checkpoint and continue.
# Wire from Codex config hooks (UserPromptSubmit, Stop, PreCompact) - see reference.md.
set -euo pipefail

ROOT="${ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SCRIPT="$ROOT/.agents/skills/relay/scripts/context_handoff.py"
TRANSFER_SCRIPT="$ROOT/.agents/skills/relay/scripts/transfer_control.py"
EVENT="${1:-UserPromptSubmit}"
THRESHOLD="${RELAY_THRESHOLD:-0.30}"

PAYLOAD="$(cat || true)"
ARGS=(--repo "$ROOT" --stdin-json --handoff-threshold "$THRESHOLD")
OFFICIAL_EVENT="UserPromptSubmit"

top_level_identity_fields() {
  printf '%s' "$PAYLOAD" | awk '
    BEGIN { depth=0; in_string=0; escape=0; token=""; pending="" }
    { data = data $0 "\n" }
    END {
      for (i=1; i<=length(data); i++) {
        c=substr(data,i,1)
        if (in_string) {
          if (escape) { token=token c; escape=0; continue }
          if (c=="\\") { escape=1; token=token c; continue }
          if (c=="\"") {
            in_string=0; j=i+1
            while (j<=length(data) && substr(data,j,1) ~ /[[:space:]]/) j++
            if (string_depth==1 && substr(data,j,1)==":") { pending=token; expecting=1 }
            else if (string_depth==1 && expecting && pending!="") { print pending "\t" token; pending=""; expecting=0 }
            token=""; continue
          }
          token=token c; continue
        }
        if (c=="\"") { in_string=1; string_depth=depth; token=""; continue }
        if (c=="{" || c=="[") { depth++; if (expecting && depth>1) { pending=""; expecting=0 }; continue }
        if (c=="}" || c=="]") { depth--; continue }
        if (expecting && c !~ /[[:space:]:]/) { pending=""; expecting=0 }
      }
      if (in_string || depth != 0) print "__INVALID__\t1"
    }'
}

emit_static_denial() {
  case "$OFFICIAL_EVENT" in
    PreToolUse) echo '{"continue":true,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"transfer runtime unavailable with ambiguous durable identity"}}' ;;
    UserPromptSubmit) echo '{"continue":true,"decision":"block","reason":"transfer runtime unavailable with ambiguous durable identity"}' ;;
    PreCompact|Stop) echo '{"continue":false,"stopReason":"transfer runtime unavailable with ambiguous durable identity"}' ;;
  esac
}

static_fallback() {
  local actor source scope source_scope state_root tombstone source_tombstone ownership owner_source owner_destination implicated pointer binding_found destination_actor tombstone_destination identity_fields actor_values source_values actor_count source_count durable_state candidate
  state_root="$ROOT/.omx/state/relay"
  identity_fields="$(top_level_identity_fields)"
  actor_values="$(printf '%s\n' "$identity_fields" | awk -F '\t' '$1=="session_id" || $1=="sessionId" || $1=="conversation_id" || $1=="conversationId" || $1=="composer_id" || $1=="composerId" || $1=="thread_id" || $1=="threadId" {print $2}')"
  source_values="$(printf '%s\n' "$identity_fields" | awk -F '\t' '$1=="source_session_id" || $1=="sourceSessionId" {print $2}')"
  actor_count="$(printf '%s\n' "$actor_values" | awk 'NF {n++} END {print n+0}')"
  source_count="$(printf '%s\n' "$source_values" | awk 'NF {n++} END {print n+0}')"
  actor="$(printf '%s\n' "$actor_values" | sed -n '1p')"
  source="$(printf '%s\n' "$source_values" | sed -n '1p')"
  durable_state=false
  [[ -e "$state_root/.ownership.json" ]] && durable_state=true
  for candidate in "$state_root"/sessions/*/.active-transfer.json "$state_root"/sessions/*/.revoked.json "$state_root"/sessions/*/transfers/*.json; do [[ -e "$candidate" ]] && durable_state=true; done
  if [[ "$identity_fields" == *$'__INVALID__\t'* || "$actor_count" -ne 1 || "$source_count" -gt 1 || -z "$actor" || "$actor" == *'\'* || "$source" == *'\'* ]]; then
    if [[ "$durable_state" == true ]]; then emit_static_denial; else echo '{"continue":true}'; fi
    return
  fi
  actor="${actor:-default}"
  source="${source:-$actor}"
  binding_found=false
  if [[ "$source" == "$actor" ]]; then
    for pointer in "$state_root"/sessions/*/.active-transfer.json; do
      [[ -f "$pointer" ]] || continue
      if grep -Fq "\"destination_session_id\":\"$actor\"" "$pointer" || grep -Fq "\"destination_session_id\": \"$actor\"" "$pointer"; then
        source="$(sed -nE 's/.*"source_session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$pointer" | head -n1)"
        binding_found=true
        break
      fi
    done
    if [[ "$binding_found" != true ]]; then
      for pointer in "$state_root"/sessions/*/transfers/*.json; do
        [[ -f "$pointer" ]] || continue
        tombstone_destination="$(sed -nE 's/.*"destination_session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$pointer" | head -n1)"
        if [[ "$tombstone_destination" == "$actor" ]]; then source="$(sed -nE 's/.*"source_session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$pointer" | head -n1)"; binding_found=true; break; fi
      done
    fi
    if [[ "$binding_found" != true ]]; then
      for tombstone in "$state_root"/sessions/*/.revoked.json; do
        [[ -f "$tombstone" ]] || continue
        tombstone_destination="$(sed -nE 's/.*"destination_session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$tombstone" | head -n1)"
        if [[ "$tombstone_destination" == "$actor" ]]; then source="$(sed -nE 's/.*"source_session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$tombstone" | head -n1)"; binding_found=true; break; fi
      done
    fi
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    scope="$(printf '%s' "$actor" | sha256sum | awk '{print substr($1,1,16)}')"
    source_scope="$(printf '%s' "$source" | sha256sum | awk '{print substr($1,1,16)}')"
  else
    scope="$(printf '%s' "$actor" | LC_ALL=C LANG=C shasum -a 256 2>/dev/null | awk '{print substr($1,1,16)}')"
    source_scope="$(printf '%s' "$source" | LC_ALL=C LANG=C shasum -a 256 2>/dev/null | awk '{print substr($1,1,16)}')"
  fi
  tombstone="$state_root/sessions/$scope/.revoked.json"
  source_tombstone="$state_root/sessions/$source_scope/.revoked.json"
  ownership="$state_root/.ownership.json"
  implicated="$binding_found"
  destination_actor="$binding_found"
  if [[ -f "$tombstone" || ( "$source" != "$actor" && -f "$source_tombstone" ) ]]; then implicated=true; fi
  if [[ -f "$ownership" ]]; then
    owner_source="$(sed -nE 's/.*"source_session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$ownership" | head -n1)"
    owner_destination="$(sed -nE 's/.*"destination_session_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$ownership" | head -n1)"
    if [[ "$actor" == "$owner_source" || "$actor" == "$owner_destination" ]]; then implicated=true; if [[ "$actor" == "$owner_destination" ]]; then destination_actor=true; else destination_actor=false; fi; fi
  fi
  if [[ "$implicated" != true ]]; then
    echo '{"continue":true}'
    return
  fi
  case "$OFFICIAL_EVENT" in
    PreToolUse) echo '{"continue":true,"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"transfer runtime unavailable after durable revocation/ownership"}}' ;;
    UserPromptSubmit)
      if [[ "$destination_actor" == true ]]; then echo '{"continue":true}'; else echo '{"continue":true,"decision":"block","reason":"transfer runtime unavailable after durable revocation/ownership"}'; fi
      ;;
    PreCompact|Stop) echo '{"continue":false,"stopReason":"transfer runtime unavailable after durable revocation/ownership"}' ;;
  esac
}

core_pretool_fallback() {
  local output status
  [[ "$OFFICIAL_EVENT" == "PreToolUse" && -f "$TRANSFER_SCRIPT" ]] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  set +e
  output="$(python3 "$TRANSFER_SCRIPT" --repo "$ROOT" hook-pretool <<<"$PAYLOAD" 2>/dev/null)"; status=$?
  set -e
  if [[ $status -eq 0 && "$output" == \{* ]]; then printf '%s\n' "$output"; return 0; fi
  return 1
}

case "$EVENT" in
  PreCompact|preCompact|pre-compact)
    OFFICIAL_EVENT="PreCompact"
    ARGS+=(--trigger pre-compact --reason "OMX PreCompact hook")
    ;;
  Stop|stop)
    OFFICIAL_EVENT="Stop"
    ARGS+=(--trigger stop --reason "OMX Stop hook")
    ;;
  UserPromptSubmit|user-prompt-submit)
    OFFICIAL_EVENT="UserPromptSubmit"
    ARGS+=(--trigger threshold --reason "OMX UserPromptSubmit context check")
    ;;
  PreToolUse|pre-tool-use)
    OFFICIAL_EVENT="PreToolUse"
    ARGS+=(--trigger manual --reason "OMX PreToolUse authority check")
    ;;
  *)
    OFFICIAL_EVENT="Stop"
    ARGS+=(--trigger manual --reason "OMX hook ($EVENT)")
    ;;
esac
ARGS+=(--official-hook-event "$OFFICIAL_EVENT")

if [[ -n "${RELAY_OBJECTIVE:-}" ]]; then
  export RELAY_OBJECTIVE
fi
if [[ -n "${RELAY_NEXT_STEP:-}" ]]; then
  export RELAY_NEXT_STEP
fi
if [[ -n "${RELAY_GOAL_OBJECTIVE:-}" ]]; then
  export RELAY_GOAL_OBJECTIVE
fi

if [[ ! -f "$SCRIPT" ]] || ! command -v python3 >/dev/null 2>&1; then
  if core_pretool_fallback; then exit 0; fi
  static_fallback
  exit 0
fi
set +e
OUTPUT="$(python3 "$SCRIPT" "${ARGS[@]}" <<<"$PAYLOAD" 2>/dev/null)"
STATUS=$?
set -e
if [[ $STATUS -ne 0 || "$OUTPUT" != \{* ]]; then
  if core_pretool_fallback; then exit 0; fi
  static_fallback
  exit 0
fi
printf '%s\n' "$OUTPUT"
