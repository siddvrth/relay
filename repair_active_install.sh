#!/usr/bin/env bash
# Repair and verify the active installed relay skill in a target repo.
set -euo pipefail

PKG="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-$(git -C "$PKG" rev-parse --show-toplevel 2>/dev/null || pwd)}"

echo "Repairing relay install in $REPO"
bash "$PKG/install.sh" "$REPO"
bash "$PKG/audit_install.sh" "$REPO"
bash "$PKG/validate.sh"
echo "relay active install repaired and verified."
