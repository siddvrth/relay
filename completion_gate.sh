#!/usr/bin/env bash
# Final completion gate for a repository using checkpoint-and-continue.
set -euo pipefail

PKG="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-$(git -C "$PKG" rev-parse --show-toplevel 2>/dev/null || pwd)}"

echo "=== checkpoint-and-continue completion gate ==="
echo "Repository: $REPO"
bash "$PKG/validate.sh"
bash "$PKG/audit_install.sh" "$REPO"
echo "=== checkpoint-and-continue completion gate passed ==="
