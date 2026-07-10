#!/usr/bin/env python3
"""Download this repository archive and extract it into the current directory."""

from __future__ import annotations

import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError

OWNER = "qqq694637644"
REPO = "skill_temple"
BRANCH = "main"
ARCHIVE_URL = f"https://github.com/{OWNER}/{REPO}/archive/refs/heads/{BRANCH}.zip"


def _safe_archive_file_name(repo: str, branch: str) -> str:
    safe_branch = "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "-"
        for character in branch
    ).strip(".-_")

    if not safe_branch:
        safe_branch = "branch"
    return f"{repo}-{safe_branch}.zip"


def _safe_target_path(destination: Path, relative_path: PurePosixPath) -> Path:
    target = destination.joinpath(*relative_path.parts).resolve()
    destination_root = destination.resolve()

    if target != destination_root and destination_root not in target.parents:
        raise RuntimeError(f"Refusing to extract unsafe path: {relative_path}")
    return target


def _download_archive(archive_path: Path) -> None:
    proxy_url = "http://127.0.0.1:10810"

    proxy_handler = urllib.request.ProxyHandler(
        {
            "http": proxy_url,
            "https": proxy_url,
        }
    )
    opener = urllib.request.build_opener(proxy_handler)

    request = urllib.request.Request(
        ARCHIVE_URL,
        headers={"User-Agent": "skill-temple-project-puller/1.0"},
    )

    try:
        with opener.open(request, timeout=60) as response:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
    except HTTPError as exc:
        raise RuntimeError(f"Download failed with HTTP {exc.code}: {ARCHIVE_URL}") from exc
    except URLError as exc:
        raise RuntimeError(f"Download failed: {exc.reason}") from exc


def _extract_archive(archive_path: Path, destination: Path) -> int:
    extracted_files = 0

    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            source_path = PurePosixPath(info.filename)
            parts = source_path.parts
            if len(parts) <= 1:
                continue

            relative_path = PurePosixPath(*parts[1:])
            target = _safe_target_path(destination, relative_path)

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted_files += 1

    return extracted_files


def main() -> None:
    destination = Path.cwd()
    print(f"Downloading {OWNER}/{REPO}@{BRANCH}")
    print(f"Target directory: {destination}")

    with tempfile.TemporaryDirectory(prefix="skill-temple-pull-") as temp_dir:
        archive_path = Path(temp_dir) / _safe_archive_file_name(REPO, BRANCH)
        _download_archive(archive_path)
        extracted_files = _extract_archive(archive_path, destination)

    print(f"Extracted {extracted_files} files into {destination}")


if __name__ == "__main__":
    main()
