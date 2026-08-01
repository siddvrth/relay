#!/usr/bin/env bash
# Install relay into the parent git repo (or cwd).
set -euo pipefail

PKG="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -gt 0 ]]; then
  REPO="$1"
elif REPO="$(git -C "$PKG" rev-parse --show-toplevel 2>/dev/null)"; then
  :
elif [[ "$(basename "$(dirname "$PKG")")" == "packages" ]]; then
  REPO="$(cd "$PKG/../.." && pwd)"
else
  REPO="$PKG"
fi

python3 - "$REPO" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import sys


repo = Path(os.path.abspath(os.path.expanduser(sys.argv[1])))
if repo.is_symlink() or not repo.is_dir():
    raise SystemExit(f"install target must be a real directory, not a symlink: {repo}")
resolved_repo = repo.resolve(strict=True)


def require_within(path: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(resolved_repo)
    except ValueError as error:
        raise SystemExit(f"install path escapes target repository: {path}") from error


for relative in (
    ".agents",
    ".agents/skills",
    ".agents/skills/relay",
    "scripts",
    "scripts/workflow",
    ".omx",
    ".omx/state",
):
    path = repo / relative
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise SystemExit(f"install namespace must be a real directory: {path}")
        require_within(path)

for relative in (
    ".gitignore",
    "scripts/workflow/relay_hook.sh",
):
    path = repo / relative
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"install file must be a real file: {path}")
        require_within(path)
PY

echo "Installing relay from $PKG into $REPO"

AGENTS_DIR_EXISTED=0
AGENTS_SKILLS_DIR_EXISTED=0
SCRIPTS_DIR_EXISTED=0
WORKFLOW_DIR_EXISTED=0
[[ -e "$REPO/.agents" || -L "$REPO/.agents" ]] && AGENTS_DIR_EXISTED=1
[[ -e "$REPO/.agents/skills" || -L "$REPO/.agents/skills" ]] && AGENTS_SKILLS_DIR_EXISTED=1
[[ -e "$REPO/scripts" || -L "$REPO/scripts" ]] && SCRIPTS_DIR_EXISTED=1
[[ -e "$REPO/scripts/workflow" || -L "$REPO/scripts/workflow" ]] && WORKFLOW_DIR_EXISTED=1
mkdir -p "$REPO/.agents/skills" "$REPO/scripts/workflow"

TARGET_SKILL="$REPO/.agents/skills/relay"
TARGET_HOOK="$REPO/scripts/workflow/relay_hook.sh"
STAGE_ROOT=""
INSTALL_COMMITTED=0

rollback_install() {
  set +e
  if [[ -n "$STAGE_ROOT" ]]; then
    rm -rf "$STAGE_ROOT"
  fi
  if [[ "$WORKFLOW_DIR_EXISTED" -eq 0 ]]; then
    rmdir "$REPO/scripts/workflow" 2>/dev/null || true
  fi
  if [[ "$SCRIPTS_DIR_EXISTED" -eq 0 ]]; then
    rmdir "$REPO/scripts" 2>/dev/null || true
  fi
  if [[ "$AGENTS_SKILLS_DIR_EXISTED" -eq 0 ]]; then
    rmdir "$REPO/.agents/skills" 2>/dev/null || true
  fi
  if [[ "$AGENTS_DIR_EXISTED" -eq 0 ]]; then
    rmdir "$REPO/.agents" 2>/dev/null || true
  fi
}

finish_install() {
  status=$?
  trap - EXIT
  if [[ "$INSTALL_COMMITTED" -ne 1 ]]; then
    rollback_install
  fi
  exit "$status"
}
trap finish_install EXIT

if [[ -e "$TARGET_SKILL" ]]; then
  PROBE="$TARGET_SKILL/.relay-write-test"
  if ! { : > "$PROBE" && rm -f "$PROBE"; } 2>/dev/null; then
    echo "Cannot update $TARGET_SKILL; directory is not writable in this environment." >&2
    echo "Run this installer from a shell with write access to the target .agents directory." >&2
    exit 1
  fi
fi

STAGE_ROOT="$(mktemp -d "$REPO/.agents/.relay-install.XXXXXX")"
STAGED_SKILL="$STAGE_ROOT/new-skill"
STAGED_HOOK="$STAGE_ROOT/new-hook"
PREVIOUS_SKILL="$STAGE_ROOT/previous-skill"
PREVIOUS_HOOK="$STAGE_ROOT/previous-hook"

# Stage and verify skill + hook before touching live paths.
cp -R "$PKG/skills/relay" "$STAGED_SKILL"
rm -f "$STAGED_SKILL/.DS_Store" "$STAGED_SKILL/scripts/.DS_Store"
rm -rf "$STAGED_SKILL/.omx" "$STAGED_SKILL/.omo"
rm -rf "$STAGED_SKILL/scripts/__pycache__"
cp "$PKG/codex/relay_hook.sh" "$STAGED_HOOK"
chmod +x "$STAGED_HOOK"
diff -qr -x __pycache__ -x '*.pyc' -x .DS_Store -x .omx -x .omo \
  "$PKG/skills/relay" "$STAGED_SKILL" >/dev/null
cmp -s "$PKG/codex/relay_hook.sh" "$STAGED_HOOK"
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "$STAGED_SKILL/scripts/"*.py
rm -rf "$STAGED_SKILL/scripts/__pycache__"
bash -n "$STAGED_HOOK"

# Swap skill + hook under one lock. On failure, restore every live path from
# the backups taken above.
python3 - "$REPO" "$STAGED_SKILL" "$STAGED_HOOK" "$TARGET_SKILL" \
  "$TARGET_HOOK" "$PREVIOUS_SKILL" "$PREVIOUS_HOOK" <<'PY'
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil


repo = Path(os.path.abspath(os.path.expanduser(os.sys.argv[1])))
staged_skill = Path(os.sys.argv[2])
staged_hook = Path(os.sys.argv[3])
target_skill = Path(os.sys.argv[4])
target_hook = Path(os.sys.argv[5])
previous_skill = Path(os.sys.argv[6])
previous_hook = Path(os.sys.argv[7])


def validate_transaction_paths() -> None:
    if repo.is_symlink() or not repo.is_dir():
        raise RuntimeError(f"install target must remain a real directory: {repo}")
    resolved_repo = repo.resolve(strict=True)
    for relative in (
        ".agents",
        ".agents/skills",
        ".agents/skills/relay",
        "scripts",
        "scripts/workflow",
        ".omx",
        ".omx/state",
    ):
        path = repo / relative
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(f"install namespace must remain a real directory: {path}")
            try:
                path.resolve(strict=True).relative_to(resolved_repo)
            except ValueError as error:
                raise RuntimeError(f"install path escaped target repository: {path}") from error
    if os.path.lexists(target_hook):
        if target_hook.is_symlink() or not target_hook.is_file():
            raise RuntimeError(f"canonical hook must remain a real file: {target_hook}")
        try:
            target_hook.resolve(strict=True).relative_to(resolved_repo)
        except ValueError as error:
            raise RuntimeError(f"canonical hook escaped target repository: {target_hook}") from error


def fsync_path(path: Path) -> None:
    flags = os.O_RDONLY
    if path.is_dir() and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


lock_path = repo / ".omx" / "state" / ".relay-install.lock"
validate_transaction_paths()
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    validate_transaction_paths()
    skill_previous = False
    skill_installed = False
    hook_previous = False
    hook_installed = False
    try:
        if target_skill.exists() or target_skill.is_symlink():
            os.replace(target_skill, previous_skill)
            skill_previous = True
        os.replace(staged_skill, target_skill)
        skill_installed = True

        if target_hook.exists() or target_hook.is_symlink():
            os.replace(target_hook, previous_hook)
            hook_previous = True
        if os.environ.get("RELAY_INSTALL_FAULT") == "canonical_hook_swap":
            raise RuntimeError("injected canonical hook swap failure")
        os.replace(staged_hook, target_hook)
        hook_installed = True
        target_hook.chmod(0o755)

        if os.environ.get("RELAY_INSTALL_FAULT") == "canonical_finalize":
            raise RuntimeError("injected canonical finalizer failure")

        fsync_path(target_skill)
        fsync_path(target_hook)
        fsync_path(target_skill.parent)
        fsync_path(target_hook.parent)
    except BaseException as failure:
        restore_errors: list[str] = []

        def restore(label: str, operation: object) -> None:
            try:
                operation()  # type: ignore[operator]
            except BaseException as error:
                restore_errors.append(f"{label}: {error}")

        if hook_installed and (target_hook.exists() or target_hook.is_symlink()):
            restore("new canonical hook", lambda: remove_path(target_hook))
        if hook_previous and (previous_hook.exists() or previous_hook.is_symlink()):
            restore("previous canonical hook", lambda: os.replace(previous_hook, target_hook))
        if skill_installed and (target_skill.exists() or target_skill.is_symlink()):
            restore("new canonical skill", lambda: remove_path(target_skill))
        if skill_previous and (previous_skill.exists() or previous_skill.is_symlink()):
            restore("previous canonical skill", lambda: os.replace(previous_skill, target_skill))

        if restore_errors:
            details = "; ".join(restore_errors)
            raise RuntimeError(
                f"install failed ({failure}); rollback was incomplete: {details}"
            ) from failure
        raise
PY

INSTALL_COMMITTED=1
trap - EXIT
rm -rf "$STAGE_ROOT" || echo "Warning: could not remove install staging directory $STAGE_ROOT" >&2

if [[ -L "$REPO/.gitignore" || -d "$REPO/.gitignore" ]]; then
  echo "Warning: skipped .gitignore update because it is not a real file" >&2
elif ! grep -q '^\.omx/' "$REPO/.gitignore" 2>/dev/null; then
  if {
    echo "" >> "$REPO/.gitignore"
    echo "# Relay runtime state" >> "$REPO/.gitignore"
    echo ".omx/" >> "$REPO/.gitignore"
  }; then
    echo "Added .omx/ to .gitignore"
  else
    echo "Warning: could not add .omx/ to $REPO/.gitignore" >&2
  fi
fi

echo "Done. Run: bash $PKG/validate.sh"
