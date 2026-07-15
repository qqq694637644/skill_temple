from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .runtime import env_value_from_environment_or_dotenv
from .workspace_operations import OperationSettings, WorkspaceOperationManager
from .workspace_patch import (
    WorkspaceToolError,
    assert_payload_size,
    assert_text_bytes,
    commit_prepared_changes,
    describe_changes,
    normalize_line_endings,
    parse_codex_patch,
    prepare_text_patch,
    prepare_write_change,
    sha256_hex,
    snapshot_files,
    target_path,
)

_DEFAULT_OUTPUT_BYTES = 200_000
_DEFAULT_FILE_BYTES = 100_000
_DEFAULT_PATCH_BYTES = 2_000_000
_DEFAULT_WRITE_BYTES = 2_000_000
_DEFAULT_CHANGED_FILES = 50
_MIN_STRUCTURED_RESPONSE_BYTES = 1024
_SEARCH_LINE_MAX_BYTES = 4_000
_SEARCH_SNIPPET_MAX_BYTES = 12_000


class LocalWorkspaceService:
    def __init__(self) -> None:
        self._operations: WorkspaceOperationManager | None = None
        self._operations_root: Path | None = None

    def root(self) -> Path:
        value = env_value_from_environment_or_dotenv("WORKSPACE_ROOT")
        if not value:
            raise WorkspaceToolError(
                "WORKSPACE_ROOT_NOT_CONFIGURED",
                "WORKSPACE_ROOT is not configured in the environment or .env file.",
                status_code=503,
            )
        root = Path(value).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise WorkspaceToolError(
                "WORKSPACE_ROOT_INVALID",
                f"WORKSPACE_ROOT does not exist or is not a directory: {root}",
                status_code=503,
            )
        return root

    async def read_files(
        self,
        *,
        paths: list[str],
        start_line: int,
        max_lines: int,
        max_bytes_per_file: int | None,
        max_bytes: int | None,
    ) -> dict[str, Any]:
        root = self.root()
        file_budget = max_bytes_per_file or _DEFAULT_FILE_BYTES
        response_budget = max_bytes or _DEFAULT_OUTPUT_BYTES
        files = [
            self._read_file_content(
                root,
                path,
                start_line=start_line,
                max_lines=max_lines,
                max_bytes=file_budget,
            )
            for path in paths
        ]
        response = {"files": files, "truncated": any(item["truncated"] for item in files)}
        return _fit_read_files_response(response, response_budget)

    async def search(
        self,
        *,
        query: str,
        regex: bool,
        case_sensitive: bool,
        paths: list[str],
        context_lines: int,
        max_matches: int,
        max_bytes: int | None,
    ) -> dict[str, Any]:
        root = self.root()
        response_budget = max_bytes or _DEFAULT_OUTPUT_BYTES
        return await self._search_workspace(
            root,
            query=query,
            regex=regex,
            case_sensitive=case_sensitive,
            paths=paths,
            context_lines=context_lines,
            max_matches=max_matches,
            max_bytes=response_budget,
        )

    async def inspect(
        self,
        *,
        paths: list[str],
        queries: list[str],
        max_depth: int,
        max_tree_entries: int,
        context_lines: int,
        max_search_matches: int,
        max_read_files: int,
        max_file_lines: int,
        max_bytes_per_file: int | None,
        max_bytes: int | None,
    ) -> dict[str, Any]:
        root = self.root()
        file_budget = max_bytes_per_file or _DEFAULT_FILE_BYTES
        response_budget = max_bytes or _DEFAULT_OUTPUT_BYTES
        tree, tree_truncated = self._tree_entries(
            root, paths, max_depth=max_depth, max_entries=max_tree_entries
        )
        searches: list[dict[str, Any]] = []
        related: dict[str, int] = {}
        for query in queries:
            result = await self._search_workspace(
                root,
                query=query,
                regex=False,
                case_sensitive=False,
                paths=paths,
                context_lines=context_lines,
                max_matches=max_search_matches,
                max_bytes=response_budget,
            )
            searches.append(
                {
                    "query": query,
                    "engine": result["engine"],
                    "matches": result["matches"],
                    "match_count": result["match_count"],
                    "truncated": result["truncated"],
                }
            )
            if max_read_files > 0:
                for match in result["matches"]:
                    related.setdefault(str(match["path"]), int(match["line_number"]))
                    if len(related) >= max_read_files:
                        break
        files = [
            self._read_file_content(
                root,
                path,
                start_line=max(1, first_line - context_lines),
                max_lines=max_file_lines,
                max_bytes=file_budget,
            )
            for path, first_line in list(related.items())[:max_read_files]
        ]
        response = {
            "tree": tree,
            "tree_truncated": tree_truncated,
            "searches": searches,
            "files": files,
            "truncated": tree_truncated
            or any(item["truncated"] for item in files)
            or any(item["truncated"] for item in searches),
        }
        return _fit_inspect_response(response, response_budget)

    async def write_file(
        self,
        *,
        path: str,
        content: str,
        mode: str,
        line_ending: str,
        expected_sha256: str | None,
        dry_run: bool,
        max_bytes: int | None,
    ) -> dict[str, Any]:
        root = self.root()
        resolved = target_path(root, path)
        previous_bytes: bytes | None = None
        if resolved.exists():
            if not resolved.is_file():
                raise WorkspaceToolError(
                    "WORKSPACE_WRITE_INVALID_PATH",
                    f"Write target exists but is not a file: {path}",
                )
            previous_bytes = resolved.read_bytes()
            assert_text_bytes(previous_bytes, path=path)
        if mode == "create_only" and previous_bytes is not None:
            raise WorkspaceToolError(
                "WORKSPACE_FILE_EXISTS",
                f"create_only target already exists: {path}",
                status_code=409,
            )
        previous_sha = sha256_hex(previous_bytes) if previous_bytes is not None else None
        if mode == "overwrite_if_sha256_matches":
            if previous_bytes is None:
                raise WorkspaceToolError(
                    "WORKSPACE_FILE_NOT_FOUND",
                    f"Hash-checked overwrite target does not exist: {path}",
                    status_code=404,
                )
            if expected_sha256 is None or previous_sha != expected_sha256.lower():
                raise WorkspaceToolError(
                    "WORKSPACE_SHA256_MISMATCH",
                    f"Current SHA-256 does not match expected_sha256 for {path}.",
                    status_code=409,
                )
        rendered = normalize_line_endings(
            content, line_ending=line_ending, previous_bytes=previous_bytes
        )
        data = rendered.encode("utf-8")
        assert_text_bytes(data, path=path)
        assert_payload_size(data, max_bytes=max_bytes or _DEFAULT_WRITE_BYTES, label="File content")
        new_sha = sha256_hex(data)
        if previous_bytes is None:
            operation = "added"
        elif previous_bytes == data:
            operation = "unchanged"
        else:
            operation = "modified"

        changed: list[dict[str, object]] = []
        diff_stat = ""
        if operation != "unchanged":
            prepared = prepare_write_change(
                path=path,
                resolved_path=resolved,
                before=previous_bytes,
                after=data,
            )
            changed, diff_stat = describe_changes(prepared)
            if not dry_run:
                commit_prepared_changes(root, prepared)
        return {
            "written": operation != "unchanged" and not dry_run,
            "dry_run": dry_run,
            "path": path,
            "operation": operation,
            "previous_sha256": previous_sha,
            "new_sha256": new_sha,
            "bytes": len(data),
            "changed_files": changed,
            "diff_stat": diff_stat,
        }

    async def apply_patch(
        self,
        *,
        patch: str,
        dry_run: bool,
        allow_delete: bool,
        max_changed_files: int | None,
        max_patch_bytes: int | None,
    ) -> dict[str, Any]:
        root = self.root()
        payload = patch.encode("utf-8")
        assert_payload_size(
            payload, max_bytes=max_patch_bytes or _DEFAULT_PATCH_BYTES, label="Patch"
        )
        changed_limit = max_changed_files or _DEFAULT_CHANGED_FILES
        operations = parse_codex_patch(
            patch, root, allow_delete=allow_delete, max_changed_files=changed_limit
        )
        paths = list(dict.fromkeys(operation.path for operation in operations))
        snapshots = snapshot_files(root, paths)
        prepared = prepare_text_patch(root, operations, snapshots)
        changed, diff_stat = describe_changes(prepared)
        if len(changed) > changed_limit:
            raise WorkspaceToolError(
                "WORKSPACE_TOO_MANY_CHANGED_FILES",
                f"Patch changes too many files: {len(changed)} > {changed_limit}.",
                status_code=413,
            )
        if not dry_run:
            commit_prepared_changes(root, prepared)
        return {
            "applied": not dry_run,
            "dry_run": dry_run,
            "changed_files": changed,
            "diff_stat": diff_stat,
        }

    async def command_start(self, **kwargs: Any) -> dict[str, Any]:
        root = self.root()
        manager = self._operation_manager()
        return await manager.start(workspace_root=root, **kwargs)

    async def command_get(self, operation_id: str) -> dict[str, Any]:
        return await self._operation_manager().get(operation_id)

    async def command_logs(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._operation_manager().logs(operation_id, **kwargs)

    async def command_cancel(self, operation_id: str) -> dict[str, Any]:
        return await self._operation_manager().cancel(operation_id)

    async def command_list(self, state: str | None) -> list[dict[str, Any]]:
        return await self._operation_manager().list_operations(state)

    async def shutdown(self) -> None:
        if self._operations is not None:
            await self._operations.shutdown()

    def _operation_manager(self) -> WorkspaceOperationManager:
        runtime_value = env_value_from_environment_or_dotenv("WORKSPACE_OPERATION_ROOT")
        runtime_root = (
            Path(runtime_value).expanduser().resolve()
            if runtime_value
            else (Path.cwd() / ".runtime" / "workspace-operations").resolve()
        )
        if self._operations is None or self._operations_root != runtime_root:
            self._operations = WorkspaceOperationManager(
                OperationSettings(
                    root=runtime_root,
                    shell=env_value_from_environment_or_dotenv("WORKSPACE_PWSH_PATH") or "pwsh",
                    default_timeout_seconds=_env_int("WORKSPACE_COMMAND_TIMEOUT_SECONDS", 120),
                    max_timeout_seconds=_env_int("WORKSPACE_COMMAND_MAX_TIMEOUT_SECONDS", 3600),
                    default_output_bytes=_env_int("WORKSPACE_COMMAND_OUTPUT_BYTES", 1_000_000),
                    max_output_bytes=_env_int("WORKSPACE_COMMAND_MAX_OUTPUT_BYTES", 10_000_000),
                    allow_network=_env_bool("WORKSPACE_ALLOW_NETWORK", False),
                )
            )
            self._operations_root = runtime_root
        return self._operations

    async def _search_workspace(
        self,
        root: Path,
        *,
        query: str,
        regex: bool,
        case_sensitive: bool,
        paths: list[str],
        context_lines: int,
        max_matches: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        if max_bytes < _MIN_STRUCTURED_RESPONSE_BYTES:
            raise WorkspaceToolError(
                "VALIDATION_ERROR",
                f"max_bytes must be at least {_MIN_STRUCTURED_RESPONSE_BYTES}.",
                status_code=422,
            )
        rg = shutil.which("rg")
        if not rg:
            raise WorkspaceToolError(
                "WORKSPACE_EXEC_FAILED",
                "ripgrep (rg) is required for workspaceSearch/workspaceInspect but was "
                "not found on PATH.",
                status_code=500,
            )
        normalized_paths = self._existing_paths(root, paths)
        args = [rg, "--json", "--line-number", "--column", "--color", "never"]
        if not regex:
            args.append("--fixed-strings")
        if not case_sensitive:
            args.append("--ignore-case")
        args.extend(["--", query, *normalized_paths])

        result = await _run_bounded_command(
            args,
            cwd=root,
            timeout_seconds=120,
            max_output_bytes=max_bytes,
        )
        if result["exit_code"] == 2:
            raise WorkspaceToolError(
                "VALIDATION_ERROR",
                "ripgrep rejected the search query: "
                + result["stderr"].decode("utf-8", errors="replace"),
                status_code=422,
            )
        stdout = result["stdout"]
        output_truncated = bool(result["truncated"])
        matches: list[dict[str, Any]] = []
        truncated = output_truncated
        for raw_line in stdout.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                truncated = True
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            raw_path = str((data.get("path") or {}).get("text") or "").replace("\\", "/")
            line_number = int(data.get("line_number") or 0)
            line_text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            submatches = data.get("submatches") or []
            column = None
            if submatches and isinstance(submatches[0], dict):
                column = int(submatches[0].get("start") or 0) + 1
            line_text, line_truncated = _clip_text_to_bytes(line_text, _SEARCH_LINE_MAX_BYTES)
            snippet_file = self._read_file_content(
                root,
                raw_path,
                start_line=max(1, line_number - context_lines),
                max_lines=(context_lines * 2) + 1,
                max_bytes=min(max_bytes, _SEARCH_SNIPPET_MAX_BYTES),
            )
            truncated = truncated or line_truncated or bool(snippet_file["truncated"])
            matches.append(
                {
                    "path": raw_path,
                    "line_number": line_number,
                    "column": column,
                    "line": line_text,
                    "snippet": snippet_file["content"] or None,
                }
            )
            if len(matches) >= max_matches:
                truncated = True
                break
        response = {
            "query": query,
            "engine": "ripgrep",
            "matches": matches,
            "match_count": len(matches),
            "truncated": truncated,
        }
        return _fit_search_response(response, max_bytes)

    def _read_file_content(
        self,
        root: Path,
        path: str,
        *,
        start_line: int,
        max_lines: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        try:
            resolved = target_path(root, path)
            if not resolved.is_file():
                raise WorkspaceToolError(
                    "WORKSPACE_FILE_NOT_FOUND",
                    f"Workspace file was not found: {path}",
                    status_code=404,
                )
            data = resolved.read_bytes()
            assert_text_bytes(data, path=path)
            lines = data.decode("utf-8").splitlines()
            start_idx = start_line - 1
            selected = lines[start_idx : start_idx + max_lines]
            output_lines: list[str] = []
            output_bytes = 0
            truncated = start_idx + len(selected) < len(lines)
            next_start_line: int | None = start_line + len(selected) if truncated else None
            for offset, line in enumerate(selected, start=start_line):
                rendered = f"{offset}: {line}"
                rendered_bytes = len((rendered + "\n").encode("utf-8"))
                if rendered_bytes > max_bytes:
                    truncated = True
                    next_start_line = offset
                    break
                if output_bytes + rendered_bytes > max_bytes:
                    truncated = True
                    next_start_line = offset
                    break
                output_lines.append(rendered)
                output_bytes += rendered_bytes
            end_line = start_line + len(output_lines) - 1 if output_lines else None
            content = "\n".join(output_lines)
            content, content_truncated = _clip_text_to_bytes(content, max_bytes)
            return {
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": len(lines),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "content": content,
                "truncated": truncated or content_truncated,
                "next_start_line": next_start_line if truncated or content_truncated else None,
                "error": None,
            }
        except Exception as exc:
            message = exc.message if isinstance(exc, WorkspaceToolError) else str(exc)
            return {
                "path": path,
                "start_line": start_line,
                "end_line": None,
                "total_lines": None,
                "bytes": None,
                "sha256": None,
                "content": "",
                "truncated": False,
                "next_start_line": None,
                "error": message,
            }

    def _tree_entries(
        self, root: Path, paths: list[str], *, max_depth: int, max_entries: int
    ) -> tuple[list[dict[str, Any]], bool]:
        entries: list[dict[str, Any]] = []
        for base in self._existing_paths(root, paths):
            base_path = target_path(root, base)
            if base_path.is_file():
                entries.append(
                    {
                        "path": base.replace("\\", "/"),
                        "type": "file",
                        "depth": 0,
                        "bytes": base_path.stat().st_size,
                    }
                )
                if len(entries) >= max_entries:
                    return entries, True
                continue
            for current, dirs, files in os.walk(base_path):
                current_path = Path(current)
                try:
                    relative_to_base = current_path.relative_to(base_path)
                    depth_from_base = len(relative_to_base.parts)
                except ValueError:
                    depth_from_base = 0
                if depth_from_base >= max_depth:
                    dirs[:] = []
                    continue
                dirs[:] = sorted(dirs)
                for dirname in dirs:
                    child = current_path / dirname
                    entries.append(
                        {
                            "path": _display_path(root, child),
                            "type": "dir",
                            "depth": depth_from_base + 1,
                            "bytes": None,
                        }
                    )
                    if len(entries) >= max_entries:
                        return entries, True
                for filename in sorted(files):
                    child = current_path / filename
                    try:
                        size = child.stat().st_size
                    except OSError:
                        size = None
                    entries.append(
                        {
                            "path": _display_path(root, child),
                            "type": "file",
                            "depth": depth_from_base + 1,
                            "bytes": size,
                        }
                    )
                    if len(entries) >= max_entries:
                        return entries, True
        return entries, False

    @staticmethod
    def _existing_paths(root: Path, paths: list[str]) -> list[str]:
        normalized = paths or ["."]
        for path in normalized:
            if not target_path(root, path).exists():
                raise WorkspaceToolError(
                    "WORKSPACE_FILE_NOT_FOUND",
                    f"Workspace path was not found: {path}",
                    status_code=404,
                )
        return normalized


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


async def _run_bounded_command(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_bytes: int,
) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    budget = {"remaining": max_output_bytes, "truncated": False}
    budget_lock = asyncio.Lock()
    stdout_task = asyncio.create_task(
        _drain_bounded_stream(proc.stdout, budget=budget, lock=budget_lock)
    )
    stderr_task = asyncio.create_task(
        _drain_bounded_stream(proc.stderr, budget=budget, lock=budget_lock)
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
    except TimeoutError as exc:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise WorkspaceToolError(
            "WORKSPACE_EXEC_TIMEOUT",
            "ripgrep search timed out.",
            status_code=504,
        ) from exc
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    return {
        "exit_code": proc.returncode or 0,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": bool(budget["truncated"]),
    }


async def _drain_bounded_stream(
    stream: asyncio.StreamReader | None,
    *,
    budget: dict[str, int | bool],
    lock: asyncio.Lock,
) -> bytes:
    if stream is None:
        return b""
    collected = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return bytes(collected)
        async with lock:
            remaining = int(budget["remaining"])
            accepted = chunk[:remaining]
            budget["remaining"] = remaining - len(accepted)
            if len(accepted) < len(chunk):
                budget["truncated"] = True
        collected.extend(accepted)


def _clip_text_to_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError as exc:
            clipped = clipped[: exc.start]
    return "", True


def _json_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fit_read_files_response(response: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    if max_bytes < _MIN_STRUCTURED_RESPONSE_BYTES:
        raise WorkspaceToolError("VALIDATION_ERROR", "max_bytes is too small.", status_code=422)
    while _json_bytes(response) > max_bytes and response["files"]:
        last = response["files"][-1]
        if last["content"]:
            last["content"] = ""
            last["truncated"] = True
            last["next_start_line"] = last["start_line"]
        else:
            response["files"].pop()
        response["truncated"] = True
    if _json_bytes(response) > max_bytes:
        raise WorkspaceToolError(
            "VALIDATION_ERROR",
            "max_bytes is too small for the response envelope.",
            status_code=422,
        )
    return response


def _fit_search_response(response: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    while _json_bytes(response) > max_bytes and response["matches"]:
        last = response["matches"][-1]
        if last["snippet"]:
            last["snippet"] = None
        elif last["line"]:
            last["line"] = ""
        else:
            response["matches"].pop()
        response["match_count"] = len(response["matches"])
        response["truncated"] = True
    if _json_bytes(response) > max_bytes:
        raise WorkspaceToolError(
            "VALIDATION_ERROR",
            "max_bytes is too small for the response envelope.",
            status_code=422,
        )
    return response


def _fit_inspect_response(response: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    while _json_bytes(response) > max_bytes:
        if response["files"]:
            last = response["files"][-1]
            if last["content"]:
                last["content"] = ""
                last["truncated"] = True
                last["next_start_line"] = last["start_line"]
            else:
                response["files"].pop()
            response["truncated"] = True
            continue
        searches_with_matches = [item for item in response["searches"] if item["matches"]]
        if searches_with_matches:
            search = searches_with_matches[-1]
            last = search["matches"][-1]
            if last["snippet"]:
                last["snippet"] = None
            elif last["line"]:
                last["line"] = ""
            else:
                search["matches"].pop()
            search["match_count"] = len(search["matches"])
            search["truncated"] = True
            response["truncated"] = True
            continue
        if response["tree"]:
            response["tree"].pop()
            response["tree_truncated"] = True
            response["truncated"] = True
            continue
        if response["searches"]:
            response["searches"].pop()
            response["truncated"] = True
            continue
        raise WorkspaceToolError(
            "VALIDATION_ERROR",
            "max_bytes is too small for the response envelope.",
            status_code=422,
        )
    return response


def _env_int(name: str, default: int) -> int:
    value = env_value_from_environment_or_dotenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise WorkspaceToolError(
            "WORKSPACE_CONFIG_INVALID", f"{name} must be an integer.", status_code=503
        ) from exc


def _env_bool(name: str, default: bool) -> bool:
    value = env_value_from_environment_or_dotenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
