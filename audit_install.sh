#!/usr/bin/env bash
# Verify that a repository's installed relay surfaces match this package.
set -euo pipefail

PKG="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-$PKG}"

failures=0

check_dir() {
  local source_dir="$1"
  local target_dir="$2"
  local label="$3"

  if [[ ! -d "$target_dir" ]]; then
    echo "MISSING $label: $target_dir"
    failures=$((failures + 1))
    return
  fi

  if diff -qr -x __pycache__ -x '*.pyc' -x .DS_Store "$source_dir" "$target_dir" >/dev/null; then
    echo "OK $label"
  else
    echo "DRIFT $label"
    diff -qr -x __pycache__ -x '*.pyc' -x .DS_Store "$source_dir" "$target_dir" || true
    failures=$((failures + 1))
  fi
}

check_file() {
  local source_file="$1"
  local target_file="$2"
  local label="$3"

  if [[ ! -f "$target_file" ]]; then
    echo "MISSING $label: $target_file"
    failures=$((failures + 1))
    return
  fi

  if cmp -s "$source_file" "$target_file"; then
    echo "OK $label"
  else
    echo "DRIFT $label"
    diff -u "$source_file" "$target_file" || true
    failures=$((failures + 1))
  fi
}

check_dir "$PKG/skills/relay" "$REPO/.agents/skills/relay" "installed skill"
check_file "$PKG/codex/relay_hook.sh" "$REPO/scripts/workflow/relay_hook.sh" "Codex hook stub"

if [[ "$failures" -ne 0 ]]; then
  echo "Install audit failed: $failures drift/missing check(s)." >&2
  exit 1
fi

echo "Install audit passed."
