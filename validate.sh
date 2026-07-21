#!/usr/bin/env bash
set -euo pipefail

PKG="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-$(cd "$PKG/../.." && pwd)}"
SKILL="$PKG/skills/checkpoint-and-continue/scripts"
SMOKE_REPO="${TMPDIR:-/tmp}/checkpoint and continue smoke $$"

echo "=== checkpoint-and-continue validate ==="
PYCACHE="${TMPDIR:-/tmp}/checkpoint-and-continue-pycache-$$"
trap 'rm -rf "$PYCACHE" "$SMOKE_REPO"' EXIT
export PYTHONPYCACHEPREFIX="$PYCACHE"

PYTHONS=()
PYTHON_COUNT=0

add_python() {
  candidate="$1"
  required="$2"

  if ! command -v "$candidate" >/dev/null 2>&1; then
    if [[ "$required" == "required" ]]; then
      echo "required Python runtime not found: $candidate" >&2
      exit 1
    fi
    return
  fi

  probe="$("$candidate" -c '
import os
import sys

if sys.version_info < (3, 10):
    raise SystemExit(2)
print(os.path.realpath(sys.executable))
print(".".join(str(part) for part in sys.version_info[:3]))
' 2>/dev/null)" || {
    status="$?"
    if [[ "$required" == "required" ]]; then
      echo "$candidate must resolve to Python 3.10 or newer (probe exit $status)" >&2
      exit 1
    fi
    echo "skipping $candidate: runtime is not Python 3.10 or newer" >&2
    return
  }

  executable="${probe%%$'\n'*}"
  version="${probe#*$'\n'}"
  if [[ "$PYTHON_COUNT" -gt 0 ]]; then
    for existing in "${PYTHONS[@]}"; do
      if [[ "$existing" == "$executable" ]]; then
        echo "Python $version: $candidate -> $executable (duplicate, skipped)"
        return
      fi
    done
  fi

  PYTHONS+=("$executable")
  PYTHON_COUNT=$((PYTHON_COUNT + 1))
  echo "Python $version: $candidate -> $executable"
}

add_python python3 required
add_python python3.10 optional
add_python python3.11 optional

for python_runtime in "${PYTHONS[@]}"; do
  "$python_runtime" -m py_compile "$SKILL"/*.py "$PKG/scripts"/*.py
done
python3 "$PKG/scripts/validate_distribution.py"
python3 "$PKG/scripts/test_release_readiness.py"
python3 "$SKILL/test_transfer_control.py" -q
python3 "$SKILL/test_transfer_integration.py" -q
python3 "$SKILL/test_transfer_hostile.py" -q
python3 "$SKILL/test_write_handoff.py"

# Smoke from a fresh installed repo so validation cannot pass because of a stale host .agents copy.
mkdir -p "$SMOKE_REPO"
bash "$PKG/install.sh" "$SMOKE_REPO" >/dev/null
bash "$PKG/audit_install.sh" "$SMOKE_REPO" >/dev/null
for python_runtime in "${PYTHONS[@]}"; do
  "$python_runtime" -m py_compile \
    "$SMOKE_REPO/.agents/skills/checkpoint-and-continue/scripts/"*.py
done
python3 "$SMOKE_REPO/.agents/skills/checkpoint-and-continue/scripts/test_write_handoff.py" >/dev/null
python3 "$SMOKE_REPO/.agents/skills/checkpoint-and-continue/scripts/test_transfer_control.py" -q >/dev/null
python3 "$SMOKE_REPO/.agents/skills/checkpoint-and-continue/scripts/test_transfer_hostile.py" -q >/dev/null
echo "fresh-install installed test suite: OK"
for session_id in a b plugin-a plugin-b; do
  python3 "$SMOKE_REPO/.agents/skills/checkpoint-and-continue/scripts/write_handoff.py" \
    --repo "$SMOKE_REPO" \
    --session-id "$session_id" \
    --update-active-task-only \
    --objective "fresh-install smoke objective" \
    --active-task "exercise installed hook envelopes" \
    --phase "validation" \
    --status "in progress" \
    --completion-criteria "official hook response is valid" \
    --remaining-work "inspect smoke response" \
    --constraints "use the installed standard-library runtime" \
    --authoritative-files "scripts/workflow/checkpoint_and_continue_hook.sh" \
    --resume-validation-command "python3 -V" \
    --resume-validation-expected "exit 0" \
    --next-action "inspect the official hook response" >/dev/null
done

python3 - "$SMOKE_REPO" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
states = {}
for path in (repo / ".omx/state/checkpoint-and-continue/sessions").glob("*/.active-task.json"):
    payload = json.loads(path.read_text(encoding="utf-8"))
    states[payload["session_id"]] = payload

assert set(states) == {"a", "b", "plugin-a", "plugin-b"}
for payload in states.values():
    assert payload["completed_work"] == []
    assert payload["decisions"] == []
    assert payload["blockers"] == []
    assert payload["validation_evidence"] == []
    assert payload["next_action"] == "inspect the official hook response"
    assert "next_step" not in payload
    assert payload["resume_validation"] == {
        "command": "python3 -V",
        "expected": "exit 0",
    }
PY
echo "fresh-install canonical optional-state seed: OK"

threshold="$(cd "$SMOKE_REPO" && printf '{"session_id":"a","context_usage_percent":31}' | bash scripts/workflow/checkpoint_and_continue_hook.sh UserPromptSubmit 2>/dev/null)"
compact="$(cd "$SMOKE_REPO" && printf '{"session_id":"b","context_usage_percent":1}' | bash scripts/workflow/checkpoint_and_continue_hook.sh PreCompact 2>/dev/null)"
capsule_count="$(find "$SMOKE_REPO/.omx/state/checkpoint-and-continue" -name '*-handoff.md' -type f | wc -l | tr -d '[:space:]')"

[[ "$threshold" == *'"hookEventName": "UserPromptSubmit"'* ]]
[[ "$threshold" == *'"additionalContext"'* ]]
[[ "$threshold" == *'Exact next action: inspect the official hook response.'* ]]
[[ "$threshold" == *'Resume validation expected: exit 0.'* ]]
[[ "$threshold" != *'fresh-install smoke objective'* ]]
[[ "$compact" == *'"continue": true'* ]]
[[ "$compact" != *'hookSpecificOutput'* ]]
[[ "$capsule_count" == "2" ]]
echo "fresh-install Codex hook lifecycle: OK"

plugin_threshold="$(cd "$SMOKE_REPO" && printf '{"session_id":"plugin-a","context_usage_percent":31}' | PLUGIN_ROOT="$PKG" ROOT="$SMOKE_REPO" bash "$PKG/hooks/checkpoint_and_continue_hook.sh" UserPromptSubmit 2>/dev/null)"
plugin_compact="$(cd "$SMOKE_REPO" && printf '{"session_id":"plugin-b","context_usage_percent":1}' | PLUGIN_ROOT="$PKG" ROOT="$SMOKE_REPO" bash "$PKG/hooks/checkpoint_and_continue_hook.sh" PreCompact 2>/dev/null)"
[[ "$plugin_threshold" == *'"hookEventName": "UserPromptSubmit"'* ]]
[[ "$plugin_threshold" == *'"additionalContext"'* ]]
[[ "$plugin_threshold" == *'Exact next action: inspect the official hook response.'* ]]
[[ "$plugin_threshold" == *'Resume validation expected: exit 0.'* ]]
[[ "$plugin_threshold" != *'fresh-install smoke objective'* ]]
[[ "$plugin_compact" == *'"continue": true'* ]]
[[ "$plugin_compact" != *'hookSpecificOutput'* ]]
echo "plugin-bundled hook lifecycle: OK"

echo "=== all checks passed ==="
