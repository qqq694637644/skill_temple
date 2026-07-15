"""Local workspace GPT Actions ported from github-gpt-actions-gateway."""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .workspace_files import LocalWorkspaceService
from .workspace_patch import WorkspaceToolError


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChangedFile(WorkspaceModel):
    path: str
    operation: str
    status: str | None = None
    previous_path: str | None = None
    additions: int = 0
    deletions: int = 0


class WorkspaceFileContent(WorkspaceModel):
    path: str
    start_line: int
    end_line: int | None = None
    total_lines: int | None = None
    bytes: int | None = None
    sha256: str | None = None
    content: str = ""
    truncated: bool = False
    next_start_line: int | None = None
    error: str | None = None


class WorkspaceReadFilesRequest(WorkspaceModel):
    paths: list[str] = Field(min_length=1, max_length=50)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=5000)
    max_bytes_per_file: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1024)


class WorkspaceReadFilesResponse(WorkspaceModel):
    files: list[WorkspaceFileContent]
    truncated: bool = False


class WorkspaceSearchMatch(WorkspaceModel):
    path: str
    line_number: int
    column: int | None = None
    line: str
    snippet: str | None = None


class WorkspaceSearchRequest(WorkspaceModel):
    query: str = Field(min_length=1, max_length=500)
    regex: bool = False
    case_sensitive: bool = False
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    context_lines: int = Field(default=2, ge=0, le=20)
    max_matches: int = Field(default=100, ge=1, le=1000)
    max_bytes: int | None = Field(default=None, ge=1024)


class WorkspaceSearchResponse(WorkspaceModel):
    query: str
    engine: Literal["ripgrep"]
    matches: list[WorkspaceSearchMatch]
    match_count: int
    truncated: bool = False


class WorkspaceTreeEntry(WorkspaceModel):
    path: str
    type: Literal["file", "dir"]
    depth: int
    bytes: int | None = None


class WorkspaceInspectRequest(WorkspaceModel):
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    queries: list[str] = Field(default_factory=list, max_length=10)
    max_depth: int = Field(default=2, ge=1, le=10)
    max_tree_entries: int = Field(default=200, ge=1, le=5000)
    context_lines: int = Field(default=2, ge=0, le=20)
    max_search_matches: int = Field(default=50, ge=1, le=1000)
    max_read_files: int = Field(default=10, ge=0, le=50)
    max_file_lines: int = Field(default=120, ge=1, le=5000)
    max_bytes_per_file: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1024)


class WorkspaceInspectSearchResult(WorkspaceModel):
    query: str
    engine: Literal["ripgrep"]
    matches: list[WorkspaceSearchMatch]
    match_count: int
    truncated: bool = False


class WorkspaceInspectResponse(WorkspaceModel):
    tree: list[WorkspaceTreeEntry]
    tree_truncated: bool = False
    searches: list[WorkspaceInspectSearchResult] = Field(default_factory=list)
    files: list[WorkspaceFileContent] = Field(default_factory=list)
    truncated: bool = False


class WorkspaceWriteFileRequest(WorkspaceModel):
    path: str = Field(min_length=1, max_length=500)
    content: str
    mode: Literal["create_only", "overwrite", "overwrite_if_sha256_matches"] = "create_only"
    encoding: Literal["utf-8"] = "utf-8"
    line_ending: Literal["preserve", "lf", "crlf"] = "preserve"
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    dry_run: bool = False
    max_bytes: int | None = Field(default=None, ge=1)


class WorkspaceWriteFileResponse(WorkspaceModel):
    written: bool
    dry_run: bool
    path: str
    operation: str
    previous_sha256: str | None = None
    new_sha256: str
    bytes: int
    changed_files: list[ChangedFile]
    diff_stat: str


class WorkspaceApplyPatchRequest(WorkspaceModel):
    patch: str = Field(min_length=1)
    dry_run: bool = False
    allow_delete: bool = False
    max_changed_files: int | None = Field(default=None, ge=1)
    max_patch_bytes: int | None = Field(default=None, ge=1)


class WorkspaceApplyPatchResponse(WorkspaceModel):
    applied: bool
    dry_run: bool
    changed_files: list[ChangedFile]
    diff_stat: str


class WorkspaceOperationSummary(WorkspaceModel):
    operation_id: str
    script_sha256: str
    script_summary: str
    state: Literal["running", "succeeded", "failed", "timed_out", "canceled", "interrupted"]
    root_pid: int | None = None
    job_assigned: bool = False
    started_at: str
    deadline_at: str
    finished_at: str | None = None
    duration_ms: int = 0
    exit_code: int | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None


class WorkspaceCommandRequest(WorkspaceModel):
    action: Literal["start", "get", "logs", "cancel", "list"] = Field(
        description="Command action. Fields not used by the selected action are ignored."
    )
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)
    script: str | None = Field(default=None, min_length=1, max_length=20000)
    timeout_seconds: int | None = Field(default=None, ge=1)
    max_output_bytes: int | None = Field(default=None, ge=1)
    allow_network: bool = False
    plain_output: bool = False
    utf8_output: bool = True
    operation_id: str | None = Field(default=None, pattern=r"^op_[0-9a-f]{16}$")
    stdout_offset: int = Field(default=0, ge=0)
    stderr_offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=50_000, ge=1, le=500_000)
    state: (
        Literal["running", "succeeded", "failed", "timed_out", "canceled", "interrupted"]
        | None
    ) = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> WorkspaceCommandRequest:
        if self.action == "start":
            missing = [
                name
                for name, value in (
                    ("idempotency_key", self.idempotency_key),
                    ("script", self.script),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"action=start requires: {', '.join(missing)}")
        elif self.action in {"get", "logs", "cancel"} and self.operation_id is None:
            raise ValueError(f"action={self.action} requires: operation_id")
        return self


class WorkspaceCommandResponse(WorkspaceModel):
    action: Literal["start", "get", "logs", "cancel", "list"]
    operation: WorkspaceOperationSummary | None = None
    operations: list[WorkspaceOperationSummary] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    next_stdout_offset: int = 0
    next_stderr_offset: int = 0
    stdout_eof: bool = False
    stderr_eof: bool = False


def _raise_http(exc: WorkspaceToolError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "suggested_next_action": "check_workspace_request",
            }
        },
    ) from exc


def register_workspace_actions(app: FastAPI) -> None:
    service = LocalWorkspaceService()
    app.state.local_workspace_service = service

    app.router.add_event_handler("shutdown", service.shutdown)

    @app.post(
        "/v1/workspace/command",
        operation_id="workspaceCommand",
        response_model=WorkspaceCommandResponse,
        summary="Start or manage a PowerShell workspace command.",
        description=(
            "Start, inspect, read logs from, list, or cancel an asynchronous pwsh 7 "
            "command running in WORKSPACE_ROOT."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_command(request: WorkspaceCommandRequest) -> WorkspaceCommandResponse:
        try:
            if request.action == "start":
                assert request.idempotency_key is not None and request.script is not None
                operation = await service.command_start(
                    idempotency_key=request.idempotency_key,
                    script=request.script,
                    timeout_seconds=request.timeout_seconds,
                    max_output_bytes=request.max_output_bytes,
                    allow_network=request.allow_network,
                    plain_output=request.plain_output,
                    utf8_output=request.utf8_output,
                )
                return WorkspaceCommandResponse(action="start", operation=operation)
            if request.action == "get":
                assert request.operation_id is not None
                return WorkspaceCommandResponse(
                    action="get", operation=await service.command_get(request.operation_id)
                )
            if request.action == "cancel":
                assert request.operation_id is not None
                return WorkspaceCommandResponse(
                    action="cancel", operation=await service.command_cancel(request.operation_id)
                )
            if request.action == "list":
                return WorkspaceCommandResponse(
                    action="list", operations=await service.command_list(request.state)
                )
            assert request.operation_id is not None
            logs = await service.command_logs(
                request.operation_id,
                stdout_offset=request.stdout_offset,
                stderr_offset=request.stderr_offset,
                max_bytes=request.max_bytes,
            )
            return WorkspaceCommandResponse(action="logs", **logs)
        except WorkspaceToolError as exc:
            _raise_http(exc)

    @app.post(
        "/v1/workspace/inspect",
        operation_id="workspaceInspect",
        response_model=WorkspaceInspectResponse,
        summary="Inspect workspace tree, search matches, and file snippets.",
        description=(
            "Inspect paths under WORKSPACE_ROOT, search with ripgrep, and read bounded "
            "snippets from matching UTF-8 files."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_inspect(request: WorkspaceInspectRequest) -> WorkspaceInspectResponse:
        try:
            return WorkspaceInspectResponse.model_validate(
                await service.inspect(**request.model_dump())
            )
        except WorkspaceToolError as exc:
            _raise_http(exc)

    @app.post(
        "/v1/workspace/search",
        operation_id="workspaceSearch",
        response_model=WorkspaceSearchResponse,
        summary="Search workspace text with ripgrep.",
        description=(
            "Search selected paths with literal or regular-expression matching and return "
            "bounded line/context results."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_search(request: WorkspaceSearchRequest) -> WorkspaceSearchResponse:
        try:
            return WorkspaceSearchResponse.model_validate(
                await service.search(**request.model_dump())
            )
        except WorkspaceToolError as exc:
            _raise_http(exc)

    @app.post(
        "/v1/workspace/read-files",
        operation_id="workspaceReadFiles",
        response_model=WorkspaceReadFilesResponse,
        summary="Read multiple UTF-8 workspace files with line numbers.",
        description=(
            "Read selected files from WORKSPACE_ROOT with line numbers, hashes, metadata, "
            "and response truncation limits."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_read_files(
        request: WorkspaceReadFilesRequest,
    ) -> WorkspaceReadFilesResponse:
        try:
            return WorkspaceReadFilesResponse.model_validate(
                await service.read_files(**request.model_dump())
            )
        except WorkspaceToolError as exc:
            _raise_http(exc)

    @app.post(
        "/v1/workspace/write-file",
        operation_id="workspaceWriteFile",
        response_model=WorkspaceWriteFileResponse,
        summary="Write one UTF-8 text file.",
        description=(
            "Create or overwrite a text file with mode, SHA-256, line-ending, dry-run, "
            "and output-size controls."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_write_file(
        request: WorkspaceWriteFileRequest,
    ) -> WorkspaceWriteFileResponse:
        try:
            payload = request.model_dump(exclude={"encoding"})
            return WorkspaceWriteFileResponse.model_validate(
                await service.write_file(**payload)
            )
        except WorkspaceToolError as exc:
            _raise_http(exc)

    @app.post(
        "/v1/workspace/apply-patch",
        operation_id="workspaceApplyPatch",
        response_model=WorkspaceApplyPatchResponse,
        summary="Apply a controlled Codex text patch.",
        description=(
            "Apply Begin Patch/Add File/Update File/Delete File text patches with dry-run "
            "and rollback on failure."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_apply_patch(
        request: WorkspaceApplyPatchRequest,
    ) -> WorkspaceApplyPatchResponse:
        try:
            return WorkspaceApplyPatchResponse.model_validate(
                await service.apply_patch(**request.model_dump())
            )
        except WorkspaceToolError as exc:
            _raise_http(exc)
