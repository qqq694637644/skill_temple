from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import skill_temple.workspace_operations as operations_module
from skill_temple.app import create_app
from skill_temple.workspace_files import (
    LocalWorkspaceService,
    _fit_read_files_response,
    _run_bounded_command,
)
from skill_temple.workspace_operations import OperationSettings, WorkspaceOperationManager
from skill_temple.workspace_patch import (
    PreparedFileChange,
    WorkspaceToolError,
    _rollback_committed_changes,
    commit_prepared_changes,
    describe_changes,
)


def _client(root: Path, operation_root: Path | None = None) -> TestClient:
    environment = {
        "WORKSPACE_ROOT": str(root),
        "WORKSPACE_OPERATION_ROOT": str(operation_root or (root / ".operations")),
    }
    environment_patch = patch.dict(os.environ, environment, clear=False)
    environment_patch.start()
    client = TestClient(create_app())
    client._workspace_environment_patch = environment_patch  # type: ignore[attr-defined]
    return client


def _close_client(client: TestClient) -> None:
    client.close()
    client._workspace_environment_patch.stop()  # type: ignore[attr-defined]


async def _wait_terminal(
    manager: WorkspaceOperationManager,
    operation_id: str,
    *,
    timeout: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = await manager.get(operation_id)
        if operation["state"] != "running":
            return operation
        await asyncio.sleep(0.01)
    raise AssertionError(f"operation did not finish: {operation_id}")


def test_write_and_patch_dry_run_never_touch_disk_or_existing_empty_directories() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        existing_dir = root / "existing-empty"
        existing_dir.mkdir()
        existing_file = root / "alpha.txt"
        existing_file.write_text("one\ntwo\n", encoding="utf-8")
        before = existing_file.stat()
        client = _client(root)
        try:
            write = client.post(
                "/v1/workspace/write-file",
                json={
                    "path": "existing-empty/new.txt",
                    "content": "content\n",
                    "mode": "create_only",
                    "dry_run": True,
                },
            )
            assert write.status_code == 200, write.text
            assert write.json()["written"] is False
            assert existing_dir.is_dir()
            assert not (existing_dir / "new.txt").exists()

            patch_response = client.post(
                "/v1/workspace/apply-patch",
                json={
                    "dry_run": True,
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Update File: alpha.txt\n"
                        "@@\n"
                        "-one\n"
                        "+ONE\n"
                        " two\n"
                        "*** Add File: existing-empty/new.txt\n"
                        "+new\n"
                        "*** End Patch\n"
                    ),
                },
            )
            assert patch_response.status_code == 200, patch_response.text
            assert patch_response.json()["applied"] is False
            assert existing_file.read_text(encoding="utf-8") == "one\ntwo\n"
            assert existing_dir.is_dir()
            assert not (existing_dir / "new.txt").exists()
            after = existing_file.stat()
            assert after.st_mtime_ns == before.st_mtime_ns
            assert after.st_ino == before.st_ino
        finally:
            _close_client(client)


def test_patch_context_failure_is_detected_before_any_file_is_written() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        target = root / "alpha.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        before = target.stat()
        client = _client(root)
        try:
            response = client.post(
                "/v1/workspace/apply-patch",
                json={
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Update File: alpha.txt\n"
                        "@@\n"
                        "-one\n"
                        "+ONE\n"
                        "*** Update File: alpha.txt\n"
                        "@@\n"
                        "-missing\n"
                        "+value\n"
                        "*** End Patch\n"
                    )
                },
            )
            assert response.status_code == 409
            assert target.read_text(encoding="utf-8") == "one\ntwo\n"
            after = target.stat()
            assert after.st_mtime_ns == before.st_mtime_ns
            assert after.st_ino == before.st_ino
        finally:
            _close_client(client)


def test_prepared_commit_restores_original_when_commit_step_fails() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        target = root / "alpha.txt"
        target.write_bytes(b"before\n")
        change = PreparedFileChange(
            path="alpha.txt",
            resolved_path=target,
            before=b"before\n",
            after=b"after\n",
        )
        real_replace = os.replace

        def fail_stage_replace(source: str | Path, destination: str | Path) -> None:
            if str(source).endswith(".stage"):
                raise OSError("injected stage replace failure")
            real_replace(source, destination)

        with patch("skill_temple.workspace_patch.os.replace", side_effect=fail_stage_replace):
            with pytest.raises(OSError, match="injected stage replace failure"):
                commit_prepared_changes(root, [change])

        assert target.read_bytes() == b"before\n"
        assert not list(root.glob(".*.stage"))
        assert not list(root.glob(".*.backup"))


def test_partial_stage_write_is_registered_and_cleaned() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "workspace"
        root.mkdir()
        target = root / "alpha.txt"
        target.write_bytes(b"before\n")
        change = PreparedFileChange(
            path="alpha.txt",
            resolved_path=target,
            before=b"before\n",
            after=b"after\n",
        )
        transaction_parent = root.parent / ".skill-temple-workspace-transactions"
        real_write_bytes = Path.write_bytes

        def partial_then_fail(path: Path, data: bytes) -> int:
            if path.suffix == ".stage":
                with path.open("wb") as handle:
                    handle.write(data[:2])
                raise OSError("injected stage write failure")
            return real_write_bytes(path, data)

        with patch.object(Path, "write_bytes", partial_then_fail):
            with pytest.raises(OSError, match="injected stage write failure"):
                commit_prepared_changes(root, [change])

        assert target.read_bytes() == b"before\n"
        assert not transaction_parent.exists()


def test_partial_cleanup_failure_preserves_committed_change() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "workspace"
        root.mkdir()
        target = root / "alpha.txt"
        target.write_bytes(b"before\n")
        change = PreparedFileChange(
            path="alpha.txt",
            resolved_path=target,
            before=b"before\n",
            after=b"after\n",
        )
        transaction_parent = root.parent / ".skill-temple-workspace-transactions"
        real_rmtree = shutil.rmtree

        def delete_backups_then_fail(
            path: str | os.PathLike[str],
            *args,
            **kwargs,
        ) -> None:
            transaction_dir = Path(path)
            backups = transaction_dir / "backups"
            if backups.exists():
                real_rmtree(backups)
            raise PermissionError("injected failure after backups were deleted")

        with patch(
            "skill_temple.workspace_patch.shutil.rmtree",
            side_effect=delete_backups_then_fail,
        ):
            with pytest.raises(WorkspaceToolError) as exc:
                commit_prepared_changes(root, [change])

        assert exc.value.code == "WORKSPACE_TRANSACTION_CLEANUP_FAILED"
        assert "committed files were left intact" in exc.value.message
        assert target.read_bytes() == b"after\n"
        assert transaction_parent.exists()


def test_rollback_without_backup_keeps_current_target() -> None:
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp) / "alpha.txt"
        target.write_bytes(b"committed\n")
        change = PreparedFileChange(
            path="alpha.txt",
            resolved_path=target,
            before=b"before\n",
            after=b"committed\n",
        )

        errors = _rollback_committed_changes([change], {})

        assert errors == [
            "alpha.txt: backup is unavailable; the current target was left intact"
        ]
        assert target.read_bytes() == b"committed\n"


def test_long_single_line_does_not_advance_continuation() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "long.txt").write_text("abcdefghij\nsecond\n", encoding="utf-8")

        result = LocalWorkspaceService()._read_file_content(
            root,
            "long.txt",
            start_line=1,
            max_lines=2,
            max_bytes=6,
        )

        assert result["content"] == ""
        assert result["end_line"] is None
        assert result["truncated"] is True
        assert result["next_start_line"] == 1


def test_response_budget_truncation_restarts_current_file() -> None:
    response = {
        "files": [
            {
                "path": "alpha.txt",
                "start_line": 5,
                "end_line": 5,
                "total_lines": 10,
                "bytes": 5000,
                "sha256": "0" * 64,
                "content": "5: " + ("x" * 2000),
                "truncated": False,
                "next_start_line": None,
                "error": None,
            }
        ],
        "truncated": False,
    }

    fitted = _fit_read_files_response(response, 1024)

    assert fitted["files"][0]["content"] == ""
    assert fitted["files"][0]["truncated"] is True
    assert fitted["files"][0]["next_start_line"] == 5


def test_newline_only_change_has_nonzero_line_counts() -> None:
    change = PreparedFileChange(
        path="script.sh",
        resolved_path=Path("script.sh"),
        before=b"echo ok",
        after=b"echo ok\n",
    )

    changed, diff_stat = describe_changes([change])

    assert changed[0]["additions"] == 1
    assert changed[0]["deletions"] == 1
    assert "+1 -1" in diff_stat


def test_read_files_returns_next_start_line_when_truncated() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "alpha.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        client = _client(root)
        try:
            response = client.post(
                "/v1/workspace/read-files",
                json={"paths": ["alpha.txt"], "start_line": 1, "max_lines": 2},
            )
            assert response.status_code == 200
            assert response.json()["files"][0]["next_start_line"] == 3
        finally:
            _close_client(client)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not available")
def test_search_regex_case_sensitive_no_match_and_bounded_output() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "alpha.txt").write_text(
            "Needle 123\nneedle 456\n" + "needle many\n" * 500,
            encoding="utf-8",
        )
        client = _client(root)
        try:
            regex = client.post(
                "/v1/workspace/search",
                json={
                    "query": "Needle [0-9]+",
                    "regex": True,
                    "case_sensitive": True,
                    "paths": ["alpha.txt"],
                },
            )
            assert regex.status_code == 200, regex.text
            assert regex.json()["match_count"] == 1
            assert regex.json()["matches"][0]["line_number"] == 1

            no_match = client.post(
                "/v1/workspace/search",
                json={
                    "query": "NEEDLE",
                    "case_sensitive": True,
                    "paths": ["alpha.txt"],
                },
            )
            assert no_match.status_code == 200
            assert no_match.json()["match_count"] == 0

            bounded = client.post(
                "/v1/workspace/search",
                json={
                    "query": "needle",
                    "paths": ["alpha.txt"],
                    "max_matches": 1000,
                    "max_bytes": 1024,
                },
            )
            assert bounded.status_code == 200, bounded.text
            assert bounded.json()["truncated"] is True
            assert len(bounded.content) <= 1024
        finally:
            _close_client(client)


def test_bounded_command_runner_does_not_collect_unlimited_stdout() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = await _run_bounded_command(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000000)"],
                cwd=Path(temp),
                timeout_seconds=10,
                max_output_bytes=2048,
            )
            assert result["exit_code"] == 0
            assert result["truncated"] is True
            assert len(result["stdout"]) + len(result["stderr"]) <= 2048

    asyncio.run(scenario())


def test_initial_operation_state_write_failure_rolls_back_all_indexes() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manager = WorkspaceOperationManager(OperationSettings(root=root / "operations"))
            with patch.object(manager, "_write_record", side_effect=OSError("disk full")):
                with pytest.raises(OSError, match="disk full"):
                    await manager.start(
                        workspace_root=root,
                        idempotency_key="state-write-failure",
                        script="Write-Output unreachable",
                        timeout_seconds=10,
                        max_output_bytes=20_000,
                        allow_network=False,
                        plain_output=True,
                        utf8_output=True,
                    )
            assert manager._records == {}
            assert manager._runtimes == {}
            assert manager._idempotency == {}

    asyncio.run(scenario())


def test_command_startup_uses_end_to_end_deadline() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process_started = False
            closed_jobs: list[bool] = []

            class SlowJob:
                def __init__(self) -> None:
                    time.sleep(0.25)
                    self.assigned = False

                def close(self) -> None:
                    closed_jobs.append(True)

            async def fake_create_subprocess_exec(*args, **kwargs):
                nonlocal process_started
                process_started = True
                raise AssertionError("process creation must not start after deadline")

            manager = WorkspaceOperationManager(OperationSettings(root=root / "operations"))
            started = time.monotonic()
            with (
                patch.object(operations_module, "WindowsJob", SlowJob),
                patch.object(
                    operations_module.asyncio,
                    "create_subprocess_exec",
                    fake_create_subprocess_exec,
                ),
            ):
                operation = await manager.start(
                    workspace_root=root,
                    idempotency_key="startup-timeout",
                    script="Write-Output unreachable",
                    timeout_seconds=0.05,
                    max_output_bytes=20_000,
                    allow_network=False,
                    plain_output=True,
                    utf8_output=True,
                )
                terminal = await _wait_terminal(manager, operation["operation_id"], timeout=1)
                elapsed = time.monotonic() - started
                assert terminal["state"] == "timed_out"
                assert elapsed < 0.2
                assert process_started is False
                await asyncio.sleep(0.3)
                assert closed_jobs == [True]
                await manager.shutdown()

    asyncio.run(scenario())


def test_running_operation_is_recovered_as_interrupted() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "operations"
        operation_dir = root / "op_0123456789abcdef"
        operation_dir.mkdir(parents=True)
        (operation_dir / "state.json").write_text(
            json.dumps(
                {
                    "operation_id": "op_0123456789abcdef",
                    "idempotency_key": "recovered-operation",
                    "request_hash": "hash",
                    "state": "running",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        manager = WorkspaceOperationManager(OperationSettings(root=root))
        recovered = asyncio.run(manager.get("op_0123456789abcdef"))
        assert recovered["state"] == "interrupted"
        assert recovered["error_code"] == "gateway_restarted"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not available")
def test_command_failed_and_idempotency_conflict() -> None:
    with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as operations:
        root = Path(temp)
        client = _client(root, Path(operations))
        try:
            with client:
                started = client.post(
                    "/v1/workspace/command",
                    json={
                        "action": "start",
                        "idempotency_key": "failed-command-key",
                        "script": "exit 7",
                        "timeout_seconds": 10,
                    },
                )
                assert started.status_code == 200, started.text
                operation_id = started.json()["operation"]["operation_id"]
                deadline = time.monotonic() + 10
                terminal = None
                while time.monotonic() < deadline:
                    current = client.post(
                        "/v1/workspace/command",
                        json={"action": "get", "operation_id": operation_id},
                    ).json()["operation"]
                    if current["state"] != "running":
                        terminal = current
                        break
                    time.sleep(0.02)
                assert terminal is not None
                assert terminal["state"] == "failed"
                assert terminal["exit_code"] == 7

                conflict = client.post(
                    "/v1/workspace/command",
                    json={
                        "action": "start",
                        "idempotency_key": "failed-command-key",
                        "script": "Write-Output different",
                        "timeout_seconds": 10,
                    },
                )
                assert conflict.status_code == 409
                assert conflict.json()["detail"]["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
        finally:
            _close_client(client)
