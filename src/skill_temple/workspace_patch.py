from __future__ import annotations

import difflib
import hashlib
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PatchKind = Literal["update", "add", "delete"]

_BINARY_PATCH_MARKERS = ("GIT binary patch", "Binary files ", "Binary file ")


class WorkspaceToolError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class TextPatchHunk:
    old_lines: list[str]
    new_lines: list[str]


@dataclass(frozen=True)
class TextPatchOperation:
    kind: PatchKind
    path: str
    hunks: list[TextPatchHunk] = field(default_factory=list)
    add_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    resolved_path: Path
    existed: bool
    data: bytes | None


@dataclass(frozen=True)
class PreparedFileChange:
    path: str
    resolved_path: Path
    before: bytes | None
    after: bytes | None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_path(root: Path, path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def assert_payload_size(data: bytes, *, max_bytes: int, label: str) -> None:
    if len(data) > max_bytes:
        raise WorkspaceToolError(
            "WORKSPACE_PAYLOAD_TOO_LARGE",
            f"{label} is too large: {len(data)} bytes > {max_bytes} bytes.",
            status_code=413,
        )


def assert_text_bytes(data: bytes, *, path: str | None = None) -> None:
    if b"\x00" in data:
        raise WorkspaceToolError(
            "WORKSPACE_BINARY_NOT_ALLOWED",
            "NUL bytes are not allowed in workspace text operations.",
            status_code=403,
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        suffix = f" Path: {path}." if path else ""
        raise WorkspaceToolError(
            "WORKSPACE_BINARY_NOT_ALLOWED",
            f"Only UTF-8 text files are allowed in workspace text operations.{suffix}",
            status_code=403,
        ) from exc


def snapshot_files(root: Path, paths: list[str]) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        resolved = target_path(root, path)
        if resolved.exists():
            if not resolved.is_file():
                raise WorkspaceToolError(
                    "WORKSPACE_INVALID_PATH",
                    f"Workspace text operations only support files: {path}",
                    status_code=400,
                )
            snapshots.append(FileSnapshot(path, resolved, True, resolved.read_bytes()))
        else:
            snapshots.append(FileSnapshot(path, resolved, False, None))
    return snapshots


def parse_codex_patch(
    patch: str,
    root: Path,
    *,
    allow_delete: bool,
    max_changed_files: int,
) -> list[TextPatchOperation]:
    payload = patch.encode("utf-8")
    assert_text_bytes(payload)
    if any(marker in patch for marker in _BINARY_PATCH_MARKERS):
        raise WorkspaceToolError(
            "WORKSPACE_BINARY_NOT_ALLOWED", "Binary patches are not allowed.", status_code=403
        )
    lines = patch.splitlines()
    if (
        not lines
        or lines[0].strip() != "*** Begin Patch"
        or lines[-1].strip() != "*** End Patch"
    ):
        raise WorkspaceToolError(
            "WORKSPACE_PATCH_INVALID",
            "Patch must start with '*** Begin Patch' and end with '*** End Patch'.",
        )

    operations: list[TextPatchOperation] = []
    paths_seen: set[str] = set()
    idx = 1
    while idx < len(lines) - 1:
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if line.startswith("*** Update File: "):
            path = line.removeprefix("*** Update File: ").strip()
            resolved = target_path(root, path)
            if not resolved.exists() or not resolved.is_file():
                raise WorkspaceToolError(
                    "WORKSPACE_PATCH_INVALID",
                    f"Update File target does not exist as a file: {path}",
                )
            body, idx = _collect_operation_body(lines, idx + 1)
            operations.append(
                TextPatchOperation(kind="update", path=path, hunks=_parse_update_hunks(body, path))
            )
        elif line.startswith("*** Add File: "):
            path = line.removeprefix("*** Add File: ").strip()
            if target_path(root, path).exists():
                raise WorkspaceToolError(
                    "WORKSPACE_PATCH_INVALID",
                    f"Add File target already exists: {path}",
                    status_code=409,
                )
            body, idx = _collect_operation_body(lines, idx + 1)
            operations.append(
                TextPatchOperation(
                    kind="add",
                    path=path,
                    add_lines=_parse_add_file_lines(body, path),
                )
            )
        elif line.startswith("*** Delete File: "):
            path = line.removeprefix("*** Delete File: ").strip()
            if not allow_delete:
                raise WorkspaceToolError(
                    "WORKSPACE_DELETE_NOT_ALLOWED",
                    f"Delete File is disabled for this request: {path}",
                    status_code=403,
                )
            resolved = target_path(root, path)
            if not resolved.exists() or not resolved.is_file():
                raise WorkspaceToolError(
                    "WORKSPACE_PATCH_INVALID",
                    f"Delete File target does not exist as a file: {path}",
                )
            body, idx = _collect_operation_body(lines, idx + 1)
            if any(item.strip() for item in body):
                raise WorkspaceToolError(
                    "WORKSPACE_PATCH_INVALID",
                    f"Delete File sections cannot contain file content: {path}",
                )
            operations.append(TextPatchOperation(kind="delete", path=path))
        else:
            raise WorkspaceToolError(
                "WORKSPACE_PATCH_INVALID", f"Unsupported patch operation: {line}"
            )
        paths_seen.add(operations[-1].path)
        if len(paths_seen) > max_changed_files:
            raise WorkspaceToolError(
                "WORKSPACE_TOO_MANY_CHANGED_FILES",
                f"Patch changes too many files: {len(paths_seen)} > {max_changed_files}.",
                status_code=413,
            )

    if not operations:
        raise WorkspaceToolError(
            "WORKSPACE_PATCH_INVALID", "Patch does not contain any file operations."
        )
    return operations


def prepare_text_patch(
    root: Path,
    operations: list[TextPatchOperation],
    snapshots: list[FileSnapshot],
) -> list[PreparedFileChange]:
    current = {snapshot.path: snapshot.data for snapshot in snapshots}
    for operation in operations:
        if operation.kind == "add":
            current[operation.path] = _join_lines(
                operation.add_lines,
                trailing_newline=bool(operation.add_lines),
            ).encode("utf-8")
        elif operation.kind == "delete":
            current[operation.path] = None
        else:
            original = current[operation.path]
            if original is None:
                raise WorkspaceToolError(
                    "WORKSPACE_PATCH_CONTEXT_MISMATCH",
                    f"Patch update target no longer exists: {operation.path}",
                    status_code=409,
                )
            assert_text_bytes(original, path=operation.path)
            original_text = (
                original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            )
            lines, trailing = _split_text_lines(original_text)
            new_lines = _apply_hunks(lines, operation.hunks, operation.path)
            current[operation.path] = _join_lines(
                new_lines,
                trailing_newline=trailing,
            ).encode("utf-8")
    return [
        PreparedFileChange(
            path=snapshot.path,
            resolved_path=snapshot.resolved_path,
            before=snapshot.data,
            after=current[snapshot.path],
        )
        for snapshot in snapshots
        if snapshot.data != current[snapshot.path]
    ]


def prepare_write_change(
    *,
    path: str,
    resolved_path: Path,
    before: bytes | None,
    after: bytes,
) -> list[PreparedFileChange]:
    if before == after:
        return []
    return [
        PreparedFileChange(
            path=path,
            resolved_path=resolved_path,
            before=before,
            after=after,
        )
    ]


def commit_prepared_changes(root: Path, changes: list[PreparedFileChange]) -> None:
    transaction_parent = root.parent / ".skill-temple-workspace-transactions"
    transaction_dir = transaction_parent / ("txn_" + secrets.token_hex(12))
    staged_dir = transaction_dir / "staged"
    backup_dir = transaction_dir / "backups"
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    created_dirs: set[Path] = set()
    committed: list[PreparedFileChange] = []
    transaction_dir.mkdir(parents=True, exist_ok=False)
    try:
        for index, change in enumerate(changes):
            if change.after is not None:
                staged_dir.mkdir(parents=True, exist_ok=True)
                temporary = staged_dir / f"{index:04d}.stage"
                staged[change.path] = temporary
                temporary.write_bytes(change.after)

        for index, change in enumerate(changes):
            target = change.resolved_path
            created_dirs.update(_missing_parent_dirs(target.parent))
            target.parent.mkdir(parents=True, exist_ok=True)
            if change.before is not None:
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / f"{index:04d}.backup"
                os.replace(target, backup)
                backups[change.path] = backup
            committed.append(change)
            if change.after is not None:
                temporary = staged[change.path]
                os.replace(temporary, target)
                staged.pop(change.path)
    except Exception as original:
        rollback_errors = _rollback_committed_changes(committed, backups)
        _remove_created_dirs(created_dirs, root)
        cleanup_error = _cleanup_transaction_dir(transaction_dir, transaction_parent)
        if rollback_errors or cleanup_error is not None:
            details = list(rollback_errors)
            if cleanup_error is not None:
                details.append(str(cleanup_error))
            raise WorkspaceToolError(
                "WORKSPACE_TRANSACTION_RECOVERY_FAILED",
                "Workspace transaction failed and cleanup was incomplete: "
                + "; ".join(details),
                status_code=500,
            ) from original
        raise
    cleanup_error = _cleanup_transaction_dir(transaction_dir, transaction_parent)
    if cleanup_error is not None:
        raise WorkspaceToolError(
            "WORKSPACE_TRANSACTION_CLEANUP_FAILED",
            "Workspace changes were committed, but transaction cleanup failed. "
            "The committed files were left intact: "
            f"{cleanup_error}",
            status_code=500,
        ) from cleanup_error


def _rollback_committed_changes(
    committed: list[PreparedFileChange],
    backups: dict[str, Path],
) -> list[str]:
    errors: list[str] = []
    for change in reversed(committed):
        target = change.resolved_path
        try:
            if change.before is not None:
                backup = backups.get(change.path)
                if backup is None or not backup.exists():
                    errors.append(
                        f"{change.path}: backup is unavailable; the current target was left intact"
                    )
                    continue
                if target.exists() and target.is_file():
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
            elif target.exists() and target.is_file():
                target.unlink()
        except OSError as exc:
            errors.append(f"{change.path}: {exc}")
    return errors


def _remove_created_dirs(created_dirs: set[Path], root: Path) -> None:
    for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
        if directory == root:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def _cleanup_transaction_dir(transaction_dir: Path, transaction_parent: Path) -> OSError | None:
    try:
        shutil.rmtree(transaction_dir)
        try:
            transaction_parent.rmdir()
        except OSError:
            pass
        return None
    except OSError as exc:
        return exc


def describe_changes(
    changes: list[PreparedFileChange],
) -> tuple[list[dict[str, object]], str]:
    changed: list[dict[str, object]] = []
    for change in changes:
        before = change.before
        after = change.after
        if before is None:
            operation = "added"
        elif after is None:
            operation = "deleted"
        else:
            operation = "modified"
        additions, deletions = _line_change_counts(before, after)
        changed.append(
            {
                "path": change.path,
                "operation": operation,
                "status": None,
                "previous_path": None,
                "additions": additions,
                "deletions": deletions,
            }
        )
    lines = [
        f"{item['path']} | +{item['additions']} -{item['deletions']} ({item['operation']})"
        for item in changed
    ]
    if changed:
        lines.append(
            f"{len(changed)} file(s) changed, "
            f"{sum(int(item['additions']) for item in changed)} insertion(s), "
            f"{sum(int(item['deletions']) for item in changed)} deletion(s)"
        )
    return changed, "\n".join(lines)


def _missing_parent_dirs(path: Path) -> set[Path]:
    missing: set[Path] = set()
    current = path
    while not current.exists():
        missing.add(current)
        if current.parent == current:
            break
        current = current.parent
    return missing


def normalize_line_endings(content: str, *, line_ending: str, previous_bytes: bytes | None) -> str:
    if line_ending == "preserve":
        if (
            previous_bytes
            and b"\r\n" in previous_bytes
            and previous_bytes.count(b"\r\n") >= previous_bytes.count(b"\n")
        ):
            line_ending = "crlf"
        else:
            return content
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if line_ending == "lf":
        return normalized
    if line_ending == "crlf":
        return normalized.replace("\n", "\r\n")
    raise WorkspaceToolError(
        "VALIDATION_ERROR",
        f"Unsupported line ending mode: {line_ending}",
        status_code=422,
    )


def _collect_operation_body(lines: list[str], start: int) -> tuple[list[str], int]:
    end = start
    prefixes = ("*** Update File: ", "*** Add File: ", "*** Delete File: ")
    while end < len(lines) - 1 and not lines[end].startswith(prefixes):
        end += 1
    return lines[start:end], end


def _parse_add_file_lines(body: list[str], path: str) -> list[str]:
    output: list[str] = []
    for line in body:
        if line == "":
            continue
        if not line.startswith("+"):
            raise WorkspaceToolError(
                "WORKSPACE_PATCH_INVALID",
                f"Add File content lines must start with '+': {path}: {line}",
            )
        output.append(line[1:])
    return output


def _parse_update_hunks(body: list[str], path: str) -> list[TextPatchHunk]:
    hunks: list[TextPatchHunk] = []
    current: list[str] | None = None
    for line in body:
        if line.startswith("@@"):
            if current is not None:
                hunks.append(_build_hunk(current, path))
            current = []
            continue
        if current is None:
            if not line.strip():
                continue
            raise WorkspaceToolError(
                "WORKSPACE_PATCH_INVALID",
                f"Update File sections must contain '@@' hunks: {path}",
            )
        if line.startswith("\\ No newline at end of file"):
            continue
        if line == "" or line[0] not in {" ", "+", "-"}:
            raise WorkspaceToolError(
                "WORKSPACE_PATCH_INVALID",
                f"Patch hunk lines must start with ' ', '+', or '-': {path}",
            )
        current.append(line)
    if current is not None:
        hunks.append(_build_hunk(current, path))
    if not hunks:
        raise WorkspaceToolError(
            "WORKSPACE_PATCH_INVALID", f"Update File operation has no hunks: {path}"
        )
    return hunks


def _build_hunk(lines: list[str], path: str) -> TextPatchHunk:
    old_lines: list[str] = []
    new_lines: list[str] = []
    for line in lines:
        marker, value = line[0], line[1:]
        if marker == " ":
            old_lines.append(value)
            new_lines.append(value)
        elif marker == "-":
            old_lines.append(value)
        elif marker == "+":
            new_lines.append(value)
    if not old_lines and not new_lines:
        raise WorkspaceToolError("WORKSPACE_PATCH_INVALID", f"Empty patch hunk: {path}")
    return TextPatchHunk(old_lines=old_lines, new_lines=new_lines)


def _apply_hunks(lines: list[str], hunks: list[TextPatchHunk], path: str) -> list[str]:
    current = list(lines)
    cursor = 0
    for hunk in hunks:
        if hunk.old_lines:
            idx = _find_subsequence(current, hunk.old_lines, cursor)
            if idx < 0 and cursor > 0:
                idx = _find_subsequence(current, hunk.old_lines, 0)
            if idx < 0:
                raise WorkspaceToolError(
                    "WORKSPACE_PATCH_CONTEXT_MISMATCH",
                    f"Patch context did not match the current file content: {path}",
                    status_code=409,
                )
            current = current[:idx] + hunk.new_lines + current[idx + len(hunk.old_lines) :]
            cursor = idx + len(hunk.new_lines)
        else:
            current = current[:cursor] + hunk.new_lines + current[cursor:]
            cursor += len(hunk.new_lines)
    return current


def _find_subsequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    for idx in range(max(start, 0), len(lines) - len(needle) + 1):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def _split_text_lines(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], False
    parts = text.split("\n")
    trailing = parts[-1] == ""
    return (parts[:-1] if trailing else parts), trailing


def _join_lines(lines: list[str], *, trailing_newline: bool) -> str:
    text = "\n".join(lines)
    return text + ("\n" if trailing_newline else "")


def _line_change_counts(before: bytes | None, after: bytes | None) -> tuple[int, int]:
    old = (
        []
        if before is None
        else before.decode("utf-8", errors="replace").splitlines(keepends=True)
    )
    new = (
        []
        if after is None
        else after.decode("utf-8", errors="replace").splitlines(keepends=True)
    )
    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new).get_opcodes():
        if tag in {"insert", "replace"}:
            additions += j2 - j1
        if tag in {"delete", "replace"}:
            deletions += i2 - i1
    return additions, deletions
