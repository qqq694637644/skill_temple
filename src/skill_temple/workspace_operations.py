from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar

from .workspace_patch import WorkspaceToolError

OperationState = Literal[
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "canceled",
    "interrupted",
]
T = TypeVar("T")
_TERMINAL_STATES = {"succeeded", "failed", "timed_out", "canceled", "interrupted"}
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

_BLOCKED_ALWAYS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgh\s+auth\b", re.IGNORECASE), "GitHub CLI authentication is not allowed."),
    (re.compile(r"\bgh\s+secret\b", re.IGNORECASE), "GitHub secret operations are not allowed."),
    (
        re.compile(r"\bGet-ChildItem\s+Env:", re.IGNORECASE),
        "Enumerating process environment variables is not allowed.",
    ),
    (
        re.compile(r"\bGet-Content\s+\$env:", re.IGNORECASE),
        "Reading environment variables as files is not allowed.",
    ),
    (re.compile(r"\bssh\b", re.IGNORECASE), "ssh is not allowed from workspaceCommand."),
    (re.compile(r"\bscp\b", re.IGNORECASE), "scp is not allowed from workspaceCommand."),
]
_NETWORK_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bInvoke-WebRequest\b", re.IGNORECASE), "Network downloads are disabled."),
    (re.compile(r"\bInvoke-RestMethod\b", re.IGNORECASE), "Network requests are disabled."),
    (re.compile(r"\bcurl\b", re.IGNORECASE), "curl is disabled when network is not allowed."),
    (re.compile(r"\bwget\b", re.IGNORECASE), "wget is disabled when network is not allowed."),
]
_ENV_ALLOWLIST = {
    "PATH",
    "Path",
    "HOME",
    "USERPROFILE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PSModulePath",
    "TERM",
}
_SENSITIVE_ENV_FRAGMENTS = ("TOKEN", "SECRET", "PRIVATE_KEY", "PASSWORD", "CREDENTIAL")


@dataclass(frozen=True)
class OperationSettings:
    root: Path
    shell: str = "pwsh"
    default_timeout_seconds: int = 120
    max_timeout_seconds: int = 3600
    default_output_bytes: int = 1_000_000
    max_output_bytes: int = 10_000_000
    allow_network: bool = False
    kill_grace_seconds: int = 5
    reader_grace_seconds: int = 2
    shutdown_seconds: int = 10
    operation_ttl_hours: int = 72


@dataclass(slots=True)
class OperationRuntime:
    record: dict[str, Any]
    started_monotonic: float
    deadline_monotonic: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None
    job: WindowsJob | None = None
    stored_bytes: int = 0


class OperationDeadlineExceededError(Exception):
    pass


class WindowsJob:
    """Kill-on-close Windows Job Object used by the source gateway."""

    def __init__(self) -> None:
        self.handle: int | None = None
        self.assigned = False
        if os.name != "nt":
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000
        ok = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise OSError(error, "SetInformationJobObject failed")
        self.handle = int(handle)

    def assign(self, pid: int) -> None:
        if os.name != "nt" or self.handle is None:
            self.assigned = True
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        process = kernel32.OpenProcess(0x0100 | 0x0001 | 0x0400, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(self.handle), process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
            self.assigned = True
        finally:
            kernel32.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        if os.name != "nt" or self.handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_bool
        kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), exit_code)

    def close(self) -> None:
        if os.name == "nt" and self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None


class WorkspaceOperationManager:
    def __init__(self, settings: OperationSettings) -> None:
        self.settings = settings
        self.root = settings.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry_lock = asyncio.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._runtimes: dict[str, OperationRuntime] = {}
        self._idempotency: dict[str, str] = {}
        self._background_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._load_records()
        self.recover_running_operations()
        self.prune_terminal_operations()

    def _load_records(self) -> None:
        for directory in self.root.glob("op_*"):
            state_path = directory / "state.json"
            if not state_path.is_file():
                continue
            try:
                record = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            operation_id = str(record.get("operation_id") or directory.name)
            self._records[operation_id] = record
            key = record.get("idempotency_key")
            if isinstance(key, str):
                self._idempotency[key] = operation_id

    def recover_running_operations(self) -> int:
        recovered = 0
        for record in self._records.values():
            if record.get("state") != "running":
                continue
            record["state"] = "interrupted"
            record["finished_at"] = _utc_now()
            record["error_code"] = "gateway_restarted"
            record["error_message"] = (
                "Gateway restarted before the command reached a terminal state."
            )
            self._write_record(record)
            recovered += 1
        return recovered

    async def start(
        self,
        *,
        workspace_root: Path,
        idempotency_key: str,
        script: str,
        timeout_seconds: int | None,
        max_output_bytes: int | None,
        allow_network: bool,
        plain_output: bool,
        utf8_output: bool,
    ) -> dict[str, Any]:
        timeout = timeout_seconds or self.settings.default_timeout_seconds
        output_limit = max_output_bytes or self.settings.default_output_bytes
        if timeout > self.settings.max_timeout_seconds:
            raise WorkspaceToolError(
                "VALIDATION_ERROR",
                f"timeout_seconds exceeds {self.settings.max_timeout_seconds}.",
                status_code=422,
            )
        if output_limit > self.settings.max_output_bytes:
            raise WorkspaceToolError(
                "VALIDATION_ERROR",
                f"max_output_bytes exceeds {self.settings.max_output_bytes}.",
                status_code=422,
            )
        _validate_script(
            script,
            allow_network=allow_network,
            server_allow_network=self.settings.allow_network,
        )
        request_payload = {
            "root": str(workspace_root),
            "script": script,
            "timeout_seconds": timeout,
            "max_output_bytes": output_limit,
            "allow_network": allow_network,
            "plain_output": plain_output,
            "utf8_output": utf8_output,
        }
        request_hash = hashlib.sha256(
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        async with self._registry_lock:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id:
                existing = self._records[existing_id]
                if existing.get("request_hash") != request_hash:
                    raise WorkspaceToolError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "The same idempotency_key was reused with a different command request.",
                        status_code=409,
                    )
                return self._public_record(existing)

            operation_id = "op_" + secrets.token_hex(8)
            started_monotonic = time.monotonic()
            started_at = _utc_now()
            deadline_at = (datetime.now(UTC) + timedelta(seconds=timeout)).isoformat()
            record: dict[str, Any] = {
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
                "script_summary": _script_summary(script),
                "state": "running",
                "root_pid": None,
                "job_assigned": False,
                "started_at": started_at,
                "deadline_at": deadline_at,
                "finished_at": None,
                "duration_ms": 0,
                "exit_code": None,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "error_code": None,
                "error_message": None,
                "plain_output": plain_output,
                "max_output_bytes": output_limit,
            }
            runtime = OperationRuntime(
                record=record,
                started_monotonic=started_monotonic,
                deadline_monotonic=started_monotonic + timeout,
            )
            self._records[operation_id] = record
            self._runtimes[operation_id] = runtime
            self._idempotency[idempotency_key] = operation_id
            try:
                self._write_record(record)
            except OSError:
                self._records.pop(operation_id, None)
                self._runtimes.pop(operation_id, None)
                self._idempotency.pop(idempotency_key, None)
                raise
            runtime.task = asyncio.create_task(
                self._run(
                    runtime,
                    workspace_root=workspace_root,
                    script=script,
                    timeout_seconds=timeout,
                    max_output_bytes=output_limit,
                    plain_output=plain_output,
                    utf8_output=utf8_output,
                ),
                name=f"workspace-command-{operation_id}",
            )
            return self._public_record(record)

    async def get(self, operation_id: str) -> dict[str, Any]:
        return self._public_record(self._require_operation(operation_id))

    async def list_operations(self, state: str | None = None) -> list[dict[str, Any]]:
        records = [
            self._public_record(record)
            for record in self._records.values()
            if state is None or record.get("state") == state
        ]
        records.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
        return records

    async def logs(
        self,
        operation_id: str,
        *,
        stdout_offset: int,
        stderr_offset: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        record = self._require_operation(operation_id)
        stdout, next_stdout = _read_log(self._stdout_path(operation_id), stdout_offset, max_bytes)
        stderr, next_stderr = _read_log(self._stderr_path(operation_id), stderr_offset, max_bytes)
        if record.get("plain_output"):
            stdout = _ANSI_ESCAPE_RE.sub("", stdout)
            stderr = _ANSI_ESCAPE_RE.sub("", stderr)
        terminal = record.get("state") in _TERMINAL_STATES
        return {
            "stdout": stdout,
            "stderr": stderr,
            "next_stdout_offset": next_stdout,
            "next_stderr_offset": next_stderr,
            "stdout_eof": terminal and next_stdout >= _file_size(self._stdout_path(operation_id)),
            "stderr_eof": terminal and next_stderr >= _file_size(self._stderr_path(operation_id)),
        }

    async def cancel(self, operation_id: str) -> dict[str, Any]:
        record = self._require_operation(operation_id)
        if record.get("state") in _TERMINAL_STATES:
            return self._public_record(record)
        runtime = self._runtimes.get(operation_id)
        if runtime is not None:
            runtime.cancel_event.set()
        return self._public_record(record)

    async def shutdown(self) -> None:
        runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            if runtime.record.get("state") == "running":
                runtime.shutdown_event.set()
        tasks = [r.task for r in runtimes if r.task is not None and not r.task.done()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.settings.shutdown_seconds)
            for task in pending:
                task.cancel()
        cleanup_tasks = [task for task in self._background_cleanup_tasks if not task.done()]
        if cleanup_tasks:
            await asyncio.wait(cleanup_tasks, timeout=self.settings.kill_grace_seconds)

    def prune_terminal_operations(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=self.settings.operation_ttl_hours)
        removed = 0
        for operation_id, record in list(self._records.items()):
            if record.get("state") not in _TERMINAL_STATES:
                continue
            try:
                finished = datetime.fromisoformat(str(record.get("finished_at")))
            except (TypeError, ValueError):
                continue
            if finished >= cutoff:
                continue
            try:
                import shutil

                shutil.rmtree(self.root / operation_id)
            except OSError:
                continue
            self._records.pop(operation_id, None)
            key = record.get("idempotency_key")
            if isinstance(key, str):
                self._idempotency.pop(key, None)
            removed += 1
        return removed

    @staticmethod
    def _remaining_seconds(runtime: OperationRuntime) -> float:
        return max(0.0, runtime.deadline_monotonic - time.monotonic())

    async def _await_before_deadline(
        self,
        runtime: OperationRuntime,
        awaitable: Awaitable[T],
        *,
        on_late_result: Callable[[asyncio.Future[T]], None] | None = None,
    ) -> T:
        future = asyncio.ensure_future(awaitable)
        try:
            remaining = self._remaining_seconds(runtime)
            if remaining > 0:
                done, _ = await asyncio.wait({future}, timeout=remaining)
                if future in done:
                    return future.result()
        except asyncio.CancelledError:
            if on_late_result is not None:
                future.add_done_callback(on_late_result)
            else:
                future.cancel()
            raise
        if on_late_result is not None:
            future.add_done_callback(on_late_result)
        else:
            future.cancel()
        raise OperationDeadlineExceededError

    def _track_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        self._background_cleanup_tasks.add(task)
        task.add_done_callback(self._background_cleanup_tasks.discard)

    async def _create_job_before_deadline(self, runtime: OperationRuntime) -> WindowsJob:
        def close_late_job(future: asyncio.Future[WindowsJob]) -> None:
            try:
                future.result().close()
            except BaseException:
                pass

        return await self._await_before_deadline(
            runtime,
            asyncio.to_thread(WindowsJob),
            on_late_result=close_late_job,
        )

    async def _create_process_before_deadline(
        self,
        runtime: OperationRuntime,
        *args: str,
        cwd: str,
        env: dict[str, str],
        creationflags: int,
        preexec_fn: Callable[[], None] | None,
    ) -> asyncio.subprocess.Process:
        def terminate_late_process(
            future: asyncio.Future[asyncio.subprocess.Process],
        ) -> None:
            try:
                process = future.result()
            except BaseException:
                return
            cleanup = asyncio.create_task(
                _terminate_process_tree(
                    process,
                    None,
                    self.settings.kill_grace_seconds,
                )
            )
            self._track_cleanup_task(cleanup)

        return await self._await_before_deadline(
            runtime,
            asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            ),
            on_late_result=terminate_late_process,
        )

    async def _assign_job_before_deadline(
        self,
        runtime: OperationRuntime,
        job: WindowsJob,
        process: asyncio.subprocess.Process,
    ) -> None:
        def finish_late_assignment(future: asyncio.Future[None]) -> None:
            try:
                future.result()
            except BaseException:
                pass
            job.terminate(1)
            job.close()

        try:
            await self._await_before_deadline(
                runtime,
                asyncio.to_thread(job.assign, process.pid),
                on_late_result=finish_late_assignment,
            )
        except (OperationDeadlineExceededError, asyncio.CancelledError):
            runtime.job = None
            await _terminate_process_tree(
                process,
                job,
                self.settings.kill_grace_seconds,
            )
            raise

    async def _run(
        self,
        runtime: OperationRuntime,
        *,
        workspace_root: Path,
        script: str,
        timeout_seconds: int,
        max_output_bytes: int,
        plain_output: bool,
        utf8_output: bool,
    ) -> None:
        operation_id = str(runtime.record["operation_id"])
        ready_path = self.root / operation_id / "job.ready"
        effective_script = "\n".join(
            [
                (
                    "while (-not (Test-Path -LiteralPath "
                    "$env:GATEWAY_JOB_READY_FILE)) { Start-Sleep -Milliseconds 10 }"
                ),
                (
                    "Remove-Item -LiteralPath $env:GATEWAY_JOB_READY_FILE -Force "
                    "-ErrorAction SilentlyContinue"
                ),
                _build_pwsh_script(script, plain_output=plain_output, utf8_output=utf8_output),
            ]
        )
        args = [
            self.settings.shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            effective_script,
        ]
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        preexec_fn = getattr(os, "setsid", None) if os.name != "nt" else None
        process_env = _sanitized_environment()
        process_env["GATEWAY_JOB_READY_FILE"] = str(ready_path)
        try:
            job = await self._create_job_before_deadline(runtime)
            runtime.job = job
            proc = await self._create_process_before_deadline(
                runtime,
                *args,
                cwd=str(workspace_root),
                env=process_env,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )
            runtime.process = proc
            try:
                await self._assign_job_before_deadline(runtime, job, proc)
            except OSError as exc:
                await _terminate_process_tree(proc, job, self.settings.kill_grace_seconds)
                raise WorkspaceToolError(
                    "WORKSPACE_EXEC_FAILED",
                    f"Unable to attach the PowerShell process to a Windows Job Object: {exc}",
                    status_code=500,
                ) from exc
            async with runtime.lock:
                runtime.record["root_pid"] = proc.pid
                runtime.record["job_assigned"] = job.assigned
            if self._remaining_seconds(runtime) <= 0:
                raise OperationDeadlineExceededError
            await self._await_before_deadline(
                runtime,
                asyncio.to_thread(ready_path.parent.mkdir, parents=True, exist_ok=True),
            )
            await self._await_before_deadline(
                runtime,
                asyncio.to_thread(ready_path.write_text, "ready", encoding="utf-8"),
            )

            stdout_task = asyncio.create_task(
                self._drain_stream(
                    runtime,
                    "stdout",
                    proc.stdout,
                    self._stdout_path(operation_id),
                    max_output_bytes,
                )
            )
            stderr_task = asyncio.create_task(
                self._drain_stream(
                    runtime,
                    "stderr",
                    proc.stderr,
                    self._stderr_path(operation_id),
                    max_output_bytes,
                )
            )
            process_task = asyncio.create_task(proc.wait())
            timeout_task = asyncio.create_task(
                asyncio.sleep(self._remaining_seconds(runtime))
            )
            cancel_task = asyncio.create_task(runtime.cancel_event.wait())
            shutdown_task = asyncio.create_task(runtime.shutdown_event.wait())
            done, pending = await asyncio.wait(
                {process_task, timeout_task, cancel_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if process_task in done:
                terminal_state: OperationState = "succeeded" if proc.returncode == 0 else "failed"
                error_code = None if proc.returncode == 0 else "command_failed"
                error_message = (
                    None
                    if proc.returncode == 0
                    else f"PowerShell exited with code {proc.returncode}."
                )
            elif cancel_task in done and runtime.cancel_event.is_set():
                terminal_state = "canceled"
                error_code = "command_canceled"
                error_message = "Command was canceled."
                await _terminate_process_tree(proc, job, self.settings.kill_grace_seconds)
            elif shutdown_task in done and runtime.shutdown_event.is_set():
                terminal_state = "interrupted"
                error_code = "gateway_shutdown"
                error_message = "Gateway shutdown interrupted the command."
                await _terminate_process_tree(proc, job, self.settings.kill_grace_seconds)
            else:
                terminal_state = "timed_out"
                error_code = "command_timeout"
                error_message = f"Command exceeded {timeout_seconds} seconds."
                await _terminate_process_tree(proc, job, self.settings.kill_grace_seconds)

            for task in pending:
                task.cancel()
            if not process_task.done():
                try:
                    await asyncio.wait_for(process_task, timeout=self.settings.kill_grace_seconds)
                except (TimeoutError, asyncio.CancelledError):
                    process_task.cancel()
            _, reader_pending = await asyncio.wait(
                {stdout_task, stderr_task}, timeout=self.settings.reader_grace_seconds
            )
            for task in reader_pending:
                task.cancel()
            await self._finish(
                runtime,
                state=terminal_state,
                exit_code=proc.returncode,
                error_code=error_code,
                error_message=error_message,
            )
        except OperationDeadlineExceededError:
            if runtime.process is not None and runtime.process.returncode is None:
                await _terminate_process_tree(
                    runtime.process,
                    runtime.job,
                    self.settings.kill_grace_seconds,
                )
            await self._finish(
                runtime,
                state="timed_out",
                exit_code=runtime.process.returncode if runtime.process is not None else None,
                error_code="command_timeout",
                error_message=f"Command exceeded {timeout_seconds} seconds during startup.",
            )
        except asyncio.CancelledError:
            if runtime.process is not None:
                await _terminate_process_tree(
                    runtime.process,
                    runtime.job,
                    self.settings.kill_grace_seconds,
                )
            await self._finish(
                runtime,
                state="interrupted",
                error_code="operation_task_canceled",
                error_message="Command task was interrupted.",
            )
            raise
        except Exception as exc:
            await self._finish(
                runtime,
                state="failed",
                exit_code=runtime.process.returncode if runtime.process else None,
                error_code=(
                    exc.code
                    if isinstance(exc, WorkspaceToolError)
                    else "command_start_failed"
                ),
                error_message=exc.message if isinstance(exc, WorkspaceToolError) else str(exc),
            )
        finally:
            if runtime.process is not None and runtime.process.returncode is None:
                await _terminate_process_tree(
                    runtime.process,
                    runtime.job,
                    self.settings.kill_grace_seconds,
                )
            try:
                ready_path.unlink()
            except FileNotFoundError:
                pass
            if runtime.job:
                runtime.job.close()
            self._runtimes.pop(operation_id, None)

    async def _drain_stream(
        self,
        runtime: OperationRuntime,
        stream_name: Literal["stdout", "stderr"],
        stream: asyncio.StreamReader | None,
        path: Path,
        max_output_bytes: int,
    ) -> None:
        if stream is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("ab")
        try:
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return
                async with runtime.lock:
                    byte_field = f"{stream_name}_bytes"
                    truncated_field = f"{stream_name}_truncated"
                    runtime.record[byte_field] = (
                        int(runtime.record.get(byte_field) or 0) + len(chunk)
                    )
                    remaining = max(0, max_output_bytes - runtime.stored_bytes)
                    accepted = chunk[:remaining]
                    if accepted:
                        handle.write(accepted)
                        handle.flush()
                        runtime.stored_bytes += len(accepted)
                    if len(accepted) < len(chunk):
                        runtime.record[truncated_field] = True
        finally:
            handle.close()

    async def _finish(
        self,
        runtime: OperationRuntime,
        *,
        state: OperationState,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        async with runtime.lock:
            if runtime.record.get("state") in _TERMINAL_STATES:
                return
            runtime.record["state"] = state
            runtime.record["finished_at"] = _utc_now()
            runtime.record["exit_code"] = exit_code
            runtime.record["duration_ms"] = round(
                (time.monotonic() - runtime.started_monotonic) * 1000
            )
            runtime.record["error_code"] = error_code
            runtime.record["error_message"] = error_message
            self._write_record(runtime.record)

    def _require_operation(self, operation_id: str) -> dict[str, Any]:
        record = self._records.get(operation_id)
        if record is None:
            raise WorkspaceToolError(
                "WORKSPACE_OPERATION_NOT_FOUND",
                "Workspace command operation was not found.",
                status_code=404,
            )
        return record

    def _write_record(self, record: dict[str, Any]) -> None:
        path = self._state_path(str(record["operation_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
        data = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _state_path(self, operation_id: str) -> Path:
        return self.root / operation_id / "state.json"

    def _stdout_path(self, operation_id: str) -> Path:
        return self.root / operation_id / "stdout.log"

    def _stderr_path(self, operation_id: str) -> Path:
        return self.root / operation_id / "stderr.log"

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        hidden = {"request_hash", "idempotency_key", "plain_output", "max_output_bytes"}
        return {key: value for key, value in record.items() if key not in hidden}


def _build_pwsh_script(script: str, *, plain_output: bool, utf8_output: bool) -> str:
    prelude: list[str] = []
    if plain_output:
        prelude.extend(
            [
                "$ProgressPreference = 'SilentlyContinue'",
                "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }",
            ]
        )
    if utf8_output:
        prelude.extend(
            [
                "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
                "$env:PYTHONIOENCODING = 'utf-8'",
                "$env:PYTHONUTF8 = '1'",
                "$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'",
                "$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'",
                "$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'",
            ]
        )
    return "\n".join([*prelude, script]) if prelude else script


def _validate_script(script: str, *, allow_network: bool, server_allow_network: bool) -> None:
    for pattern, message in _BLOCKED_ALWAYS:
        if pattern.search(script):
            raise WorkspaceToolError("WORKSPACE_SCRIPT_REJECTED", message, status_code=403)
    if allow_network and not server_allow_network:
        raise WorkspaceToolError(
            "WORKSPACE_SCRIPT_REJECTED",
            "Network access is disabled by server configuration.",
            status_code=403,
        )
    if not allow_network:
        for pattern, message in _NETWORK_BLOCKED:
            if pattern.search(script):
                raise WorkspaceToolError("WORKSPACE_SCRIPT_REJECTED", message, status_code=403)


def _sanitized_environment() -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key not in _ENV_ALLOWLIST and upper not in _ENV_ALLOWLIST:
            continue
        if any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS):
            continue
        clean[key] = value
    clean.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            "GITHUB_APP_PRIVATE_KEY": "",
            "GPT_ACTION_SECRET": "",
        }
    )
    return clean


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
    job: WindowsJob | None,
    grace_seconds: int,
) -> None:
    if job is not None:
        job.terminate(1)
    if os.name == "nt" and proc.pid:
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=max(1, grace_seconds))
        except (FileNotFoundError, TimeoutError):
            pass
    elif proc.returncode is None:
        try:
            killpg = getattr(os, "killpg", None)
            getpgid = getattr(os, "getpgid", None)
            if callable(killpg) and callable(getpgid):
                killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
        except (ProcessLookupError, PermissionError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(1, grace_seconds))
    except TimeoutError:
        pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _script_summary(script: str) -> str:
    return " ".join(script.strip().split())[:200]


def _read_log(path: Path, offset: int, max_bytes: int) -> tuple[str, int]:
    if not path.is_file():
        return "", offset
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(max_bytes)
        next_offset = handle.tell()
    return data.decode("utf-8", errors="replace"), next_offset


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
