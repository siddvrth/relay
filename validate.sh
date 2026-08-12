#!/usr/bin/env bash
set -euo pipefail

PKG="$(cd "$(dirname "$0")" && pwd)"
SKILL="$PKG/skills/relay/scripts"

echo "=== relay validate ==="
PYCACHE="${TMPDIR:-/tmp}/relay-pycache-$$"
INSTALL_ROOT="${TMPDIR:-/tmp}/relay-marketplace-$$"
CODEX_HOME_TEST="${TMPDIR:-/tmp}/relay-codex-home-$$"
SMOKE_REPO="${TMPDIR:-/tmp}/relay-install-smoke-$$"
trap 'rm -rf "$PYCACHE" "$INSTALL_ROOT" "$CODEX_HOME_TEST" "$SMOKE_REPO"' EXIT
export PYTHONPYCACHEPREFIX="$PYCACHE"

PYTHONS=()
for candidate in python3 python3.10 python3.11; do
  if ! command -v "$candidate" >/dev/null 2>&1; then
    continue
  fi
  executable="$(command -v "$candidate")"
  version="$($executable -c 'import sys; print(sys.version_info[:2])')"
  if [[ "$version" != "(3, 10)" && "$version" != "(3, 11)" && "$version" != "(3, 12)" && "$version" != "(3, 13)" && "$version" != "(3, 14)" ]]; then
    continue
  fi
  duplicate=false
  for existing in "${PYTHONS[@]-}"; do
    [[ "$existing" == "$executable" ]] && duplicate=true
  done
  if [[ "$duplicate" == false ]]; then
    PYTHONS+=("$executable")
    echo "Python $version: $candidate -> $executable"
  fi
done
[[ "${#PYTHONS[@]}" -gt 0 ]] || { echo "Python 3.10+ is required" >&2; exit 1; }

for python_runtime in "${PYTHONS[@]}"; do
  "$python_runtime" -m py_compile "$SKILL"/*.py "$PKG/scripts"/*.py
done

python3 "$PKG/scripts/validate_distribution.py"
python3 "$SKILL/test_context_usage.py" -q
python3 "$SKILL/test_relay.py" -q
bash -n "$PKG/hooks/relay_hook.sh"

if command -v codex >/dev/null 2>&1; then
  mkdir -p \
    "$INSTALL_ROOT/.agents/plugins" \
    "$INSTALL_ROOT/plugins/relay/skills/relay/scripts" \
    "$INSTALL_ROOT/plugins/relay/skills/relay/agents" \
    "$CODEX_HOME_TEST"
  cp -R "$PKG/.codex-plugin" "$PKG/hooks" "$INSTALL_ROOT/plugins/relay/"
  cp "$PKG/skills/relay/SKILL.md" "$INSTALL_ROOT/plugins/relay/skills/relay/"
  cp "$PKG/skills/relay/agents/openai.yaml" "$INSTALL_ROOT/plugins/relay/skills/relay/agents/"
  cp "$PKG/skills/relay/scripts/"*.py "$INSTALL_ROOT/plugins/relay/skills/relay/scripts/"
  python3 - "$INSTALL_ROOT/.agents/plugins/marketplace.json" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "name": "relay-local",
            "plugins": [
                {
                    "name": "relay",
                    "source": {"source": "local", "path": "./plugins/relay"},
                }
            ],
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  CODEX_HOME="$CODEX_HOME_TEST" codex plugin marketplace add "$INSTALL_ROOT" --json >/dev/null
  installed_json="$(CODEX_HOME="$CODEX_HOME_TEST" codex plugin add relay@relay-local --json)"
  installed_path="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["installedPath"])' "$installed_json")"
  installed_list="$(CODEX_HOME="$CODEX_HOME_TEST" codex plugin list --json)"
  python3 -c 'import json,sys; assert any(item.get("name") == "relay" for item in json.loads(sys.argv[1]).get("installed", []))' "$installed_list"
  test -f "$installed_path/hooks/hooks.json"
  test -f "$installed_path/skills/relay/scripts/relay.py"
  mkdir -p "$SMOKE_REPO"
  git init -q "$SMOKE_REPO"
  installed_hook="$(cd "$SMOKE_REPO" && RELAY_CODEX_APP_TRANSPORT=disabled PLUGIN_ROOT="$installed_path" ROOT="$SMOKE_REPO" bash "$installed_path/hooks/relay_hook.sh" UserPromptSubmit <<< '{"session_id":"install-smoke"}')"
  python3 -c 'import json,sys; assert json.loads(sys.argv[1]) == {"continue": True}' "$installed_hook"
  echo "clean Codex plugin install: OK"
else
  echo "clean Codex plugin install: SKIPPED (codex binary not found)"
fi

if [[ "${RELAY_RUN_REAL_SMOKE:-0}" == "1" ]]; then
  python3 "$SKILL/smoke_codex_app_transport.py"
else
  echo "real local app-server smoke: SKIPPED (set RELAY_RUN_REAL_SMOKE=1)"
fi

echo "=== all checks passed ==="
