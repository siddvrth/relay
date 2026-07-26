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
    ".agents/archived-skills",
    "scripts",
    "scripts/workflow",
    ".omx",
    ".omx/state",
    ".cursor",
    ".cursor/hooks",
    ".cursor/hooks/state",
):
    path = repo / relative
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_dir():
            raise SystemExit(f"install namespace must be a real directory: {path}")
        require_within(path)

for relative in (
    ".gitignore",
    "scripts/workflow/relay_hook.sh",
    ".cursor/hooks/relay-gate.mjs",
    ".cursor/hooks/state/relay-gate.json",
    ".cursor/hooks.json",
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

# Fully stage and verify both canonical surfaces before moving any live path.
cp -R "$PKG/skills/relay" "$STAGED_SKILL"
rm -f "$STAGED_SKILL/.DS_Store" "$STAGED_SKILL/scripts/.DS_Store"
rm -rf "$STAGED_SKILL/scripts/__pycache__"
cp "$PKG/codex/relay_hook.sh" "$STAGED_HOOK"
chmod +x "$STAGED_HOOK"
diff -qr -x __pycache__ -x '*.pyc' -x .DS_Store \
  "$PKG/skills/relay" "$STAGED_SKILL" >/dev/null
cmp -s "$PKG/codex/relay_hook.sh" "$STAGED_HOOK"
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "$STAGED_SKILL/scripts/"*.py
bash -n "$STAGED_HOOK"

# Swap both canonical surfaces, publish legacy state, and archive the legacy
# skill under one lock. Any failure restores every live surface byte-for-byte.
# No migration state changes occur until canonical staging succeeds.
python3 - "$REPO" "$STAGED_SKILL" "$STAGED_HOOK" "$TARGET_SKILL" \
  "$TARGET_HOOK" "$PREVIOUS_SKILL" "$PREVIOUS_HOOK" <<'PY'
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


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
        ".agents/archived-skills",
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda value: str(value.relative_to(path))):
        relative = str(child.relative_to(path)).encode("utf-8", errors="surrogateescape")
        digest.update(relative)
        if child.is_symlink():
            digest.update(b"L")
            digest.update(os.readlink(child).encode("utf-8", errors="surrogateescape"))
        elif child.is_file():
            digest.update(b"F")
            with child.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        elif child.is_dir():
            digest.update(b"D")
    return digest.hexdigest()


def valid_legacy_checkpoint(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return "## Objective" in text and "## Next Action" in text


def newest_valid_legacy(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    candidates = sorted(
        (path for path in root.rglob("*-handoff.md") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name, str(path)),
        reverse=True,
    )
    return next((path for path in candidates if valid_legacy_checkpoint(path)), None)


def read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def migration_is_complete(final_dir: Path, source: Path, source_sha256: str) -> bool:
    provenance = read_json(final_dir / "migration.json")
    if not provenance:
        return False
    imported = final_dir / source.name
    return (
        provenance.get("source") == str(source)
        and provenance.get("source_sha256") == source_sha256
        and provenance.get("imported_path") == str(imported)
        and imported.is_file()
        and file_sha256(imported) == source_sha256
    )


def newest_canonical_mtime(root: Path) -> int | None:
    if not root.is_dir():
        return None
    mtimes = [
        path.stat().st_mtime_ns
        for path in root.rglob("*-handoff.md")
        if path.is_file()
        and not any(part.startswith(".legacy-import-") for part in path.parts)
    ]
    return max(mtimes) if mtimes else None


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


LEGACY_STATE_NAMESPACES = ("session-continuity", "checkpoint-and-continue")
LEGACY_SKILL_NAMES = ("session-continuity", "checkpoint-and-continue")


def newest_valid_legacy_source() -> tuple[Path, str] | None:
    best: tuple[int, str, str, Path] | None = None
    for namespace in LEGACY_STATE_NAMESPACES:
        source = newest_valid_legacy(repo / ".omx" / "state" / namespace)
        if source is None:
            continue
        key = (source.stat().st_mtime_ns, source.name, str(source))
        if best is None or key > best[:3]:
            best = (*key, source, namespace)
    if best is None:
        return None
    return best[3], best[4]


def migration_stage(sessions_root: Path, final_dir: Path) -> Path | None:
    legacy = newest_valid_legacy_source()
    if legacy is None:
        return None
    source, legacy_namespace = legacy
    source_sha256 = file_sha256(source)
    if migration_is_complete(final_dir, source, source_sha256):
        return None
    canonical_mtime = newest_canonical_mtime(final_dir.parent.parent)
    if canonical_mtime is not None and canonical_mtime >= source.stat().st_mtime_ns:
        return None

    temporary = Path(tempfile.mkdtemp(prefix=".legacy-import-", dir=sessions_root))
    try:
        imported = temporary / source.name
        shutil.copy2(source, imported)
        if file_sha256(imported) != source_sha256:
            raise RuntimeError("legacy checkpoint checksum changed during copy")
        provenance = {
            "migration_version": 1,
            "legacy_namespace": legacy_namespace,
            "source": str(source),
            "source_sha256": source_sha256,
            "source_mtime_ns": source.stat().st_mtime_ns,
            "imported_path": str(final_dir / source.name),
            "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        provenance_path = temporary / "migration.json"
        provenance_path.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        fsync_path(imported)
        fsync_path(provenance_path)
        fsync_path(temporary)
        return temporary
    except BaseException:
        remove_path(temporary)
        raise


def unique_previous_import(sessions_root: Path, final_dir: Path) -> Path:
    previous_sha = tree_sha256(final_dir)[:16] if final_dir.is_dir() else "partial"
    candidate = sessions_root / f".legacy-import-previous-{previous_sha}"
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = sessions_root / f".legacy-import-previous-{previous_sha}-{counter}"
        counter += 1
    return candidate


def unique_legacy_archive(active: Path, archive_root: Path, skill_name: str) -> Path:
    candidate = archive_root / skill_name
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    suffix = (
        tree_sha256(active)[:16]
        if active.is_dir() and not active.is_symlink()
        else "legacy"
    )
    candidate = archive_root / f"{skill_name}-{suffix}"
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = archive_root / f"{skill_name}-{suffix}-{counter}"
        counter += 1
    return candidate


lock_path = repo / ".omx" / "state" / ".relay-install.lock"
validate_transaction_paths()
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    validate_transaction_paths()
    canonical_root = repo / ".omx" / "state" / "relay"
    sessions_root = canonical_root / "sessions"
    canonical_root_existed = canonical_root.exists()
    sessions_root_existed = sessions_root.exists()
    sessions_root.mkdir(parents=True, exist_ok=True)
    final_dir = sessions_root / "legacy-import"
    temporary: Path | None = None
    previous_import: Path | None = None
    migration_published = False
    archived_legacies: list[tuple[Path, Path]] = []
    archive_root = repo / ".agents" / "archived-skills"
    archive_root_existed = archive_root.exists() or archive_root.is_symlink()
    skill_previous = False
    skill_installed = False
    hook_previous = False
    hook_installed = False
    try:
        if archive_root.is_symlink():
            raise RuntimeError(f"refusing symlinked legacy archive root: {archive_root}")
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

        temporary = migration_stage(sessions_root, final_dir)
        if temporary is not None:
            if final_dir.exists() or final_dir.is_symlink():
                previous_import = unique_previous_import(sessions_root, final_dir)
                os.replace(final_dir, previous_import)
            if os.environ.get("RELAY_INSTALL_FAULT") == "migration_publish":
                raise RuntimeError("injected migration publish failure")
            os.replace(temporary, final_dir)
            migration_published = True

        for legacy_skill_name in LEGACY_SKILL_NAMES:
            active_legacy = repo / ".agents" / "skills" / legacy_skill_name
            if active_legacy.exists() or active_legacy.is_symlink():
                archive_root.mkdir(parents=True, exist_ok=True)
                archived_legacy = unique_legacy_archive(
                    active_legacy, archive_root, legacy_skill_name
                )
                os.replace(active_legacy, archived_legacy)
                archived_legacies.append((archived_legacy, active_legacy))

        if os.environ.get("RELAY_INSTALL_FAULT") == "combined_finalize":
            raise RuntimeError("injected combined finalizer failure")

        fsync_path(sessions_root)
        if archived_legacies:
            fsync_path(archive_root)
        if previous_import is not None and (
            previous_import.exists() or previous_import.is_symlink()
        ):
            try:
                remove_path(previous_import)
            except OSError as error:
                print(
                    f"Warning: could not remove committed migration backup {previous_import}: {error}",
                    file=os.sys.stderr,
                )
    except BaseException as failure:
        restore_errors: list[str] = []

        def restore(label: str, operation: object) -> None:
            try:
                operation()  # type: ignore[operator]
            except BaseException as error:
                restore_errors.append(f"{label}: {error}")

        for archived_legacy, active_legacy in reversed(archived_legacies):
            if archived_legacy.exists() or archived_legacy.is_symlink():
                restore(
                    f"legacy skill {active_legacy.name}",
                    lambda archived=archived_legacy, active=active_legacy: os.replace(
                        archived, active
                    ),
                )
        if migration_published and (final_dir.exists() or final_dir.is_symlink()):
            restore("published legacy import", lambda: remove_path(final_dir))
        if previous_import is not None and (
            previous_import.exists() or previous_import.is_symlink()
        ):
            restore("previous legacy import", lambda: os.replace(previous_import, final_dir))
        if temporary is not None and (temporary.exists() or temporary.is_symlink()):
            restore("migration staging", lambda: remove_path(temporary))

        if hook_installed and (target_hook.exists() or target_hook.is_symlink()):
            restore("new canonical hook", lambda: remove_path(target_hook))
        if hook_previous and (previous_hook.exists() or previous_hook.is_symlink()):
            restore("previous canonical hook", lambda: os.replace(previous_hook, target_hook))
        if skill_installed and (target_skill.exists() or target_skill.is_symlink()):
            restore("new canonical skill", lambda: remove_path(target_skill))
        if skill_previous and (previous_skill.exists() or previous_skill.is_symlink()):
            restore("previous canonical skill", lambda: os.replace(previous_skill, target_skill))

        if not archive_root_existed and archive_root.is_dir():
            restore("new archive root", archive_root.rmdir)
        if not sessions_root_existed and sessions_root.is_dir():
            restore("new sessions root", sessions_root.rmdir)
        if not canonical_root_existed and canonical_root.is_dir():
            restore("new canonical state root", canonical_root.rmdir)

        if restore_errors:
            details = "; ".join(restore_errors)
            raise RuntimeError(
                f"install failed ({failure}); rollback was incomplete: {details}"
            ) from failure
        raise
PY

# From here onward the four live surfaces are committed. Remaining compatibility
# cleanup is best-effort and cannot turn a successful transaction into failure.
INSTALL_COMMITTED=1
trap - EXIT
rm -rf "$STAGE_ROOT" || echo "Warning: could not remove install staging directory $STAGE_ROOT" >&2

CURSOR_CLEANUP_SAFE=1
for path in \
  "$REPO/.cursor" \
  "$REPO/.cursor/hooks" \
  "$REPO/.cursor/hooks/state" \
  "$REPO/.cursor/hooks/relay-gate.mjs" \
  "$REPO/.cursor/hooks/state/relay-gate.json" \
  "$REPO/.cursor/hooks.json"; do
  if [[ -L "$path" ]]; then
    CURSOR_CLEANUP_SAFE=0
    echo "Warning: skipped compatibility cleanup through symlink $path" >&2
    break
  fi
done
if [[ "$CURSOR_CLEANUP_SAFE" -eq 1 ]]; then
  rm -f "$REPO/.cursor/hooks/relay-gate.mjs" \
    "$REPO/.cursor/hooks/state/relay-gate.json" \
    || echo "Warning: could not remove pre-Codex compatibility files" >&2
  if grep -q 'relay-gate\.mjs' "$REPO/.cursor/hooks.json" 2>/dev/null; then
    echo "Warning: remove legacy relay-gate.mjs entries from $REPO/.cursor/hooks.json" >&2
  fi
fi

if [[ -L "$REPO/.gitignore" || -d "$REPO/.gitignore" ]]; then
  echo "Warning: skipped .gitignore update because it is not a real file" >&2
elif ! grep -q '^\.omx/' "$REPO/.gitignore" 2>/dev/null; then
  if {
    echo "" >> "$REPO/.gitignore"
    echo "# Checkpoint-and-continue runtime state" >> "$REPO/.gitignore"
    echo ".omx/" >> "$REPO/.gitignore"
  }; then
    echo "Added .omx/ to .gitignore"
  else
    echo "Warning: could not add .omx/ to $REPO/.gitignore" >&2
  fi
fi

echo "Done. Run: bash $PKG/validate.sh"
