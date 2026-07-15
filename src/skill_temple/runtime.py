"""Simple Codex-style Skill loading for GPT Actions."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

DOTENV_FILE_NAME = ".env"
_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKTICK_PATH_RE = re.compile(r"`((?:docs|references|scripts|assets)/[^`\r\n]+)`")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


class SkillRuntimeError(RuntimeError):
    """Base error for Skill runtime failures."""


class SkillNotFoundError(SkillRuntimeError):
    """Raised when a requested Skill is unavailable."""


class SkillPathError(SkillRuntimeError):
    """Raised when a Skill-relative path is invalid or missing."""


@dataclass(frozen=True)
class Skill:
    skill_id: str
    root: Path
    name: str
    description: str
    content_hash: str

    @property
    def entrypoint(self) -> str:
        return "SKILL.md"


def load_runtime(skills_dir: str | Path | None = None) -> SkillRuntime:
    return SkillRuntime(_resolve_skills_dir(skills_dir))


def _resolve_skills_dir(skills_dir: str | Path | None) -> Path:
    if skills_dir:
        return Path(skills_dir).expanduser().resolve()

    configured = env_value_from_environment_or_dotenv("SKILL_TEMPLE_SKILLS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    local = Path.cwd() / "skills"
    if local.is_dir():
        return local.resolve()

    with resources.as_file(resources.files("skill_temple") / "example_skills") as path:
        return path.resolve()


def env_value_from_environment_or_dotenv(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    return _read_dotenv_file(Path.cwd() / DOTENV_FILE_NAME).get(name)


def _read_dotenv_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def _parse_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillRuntimeError(f"SKILL.md is missing YAML frontmatter: {path}")
    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillRuntimeError(f"SKILL.md frontmatter is not closed: {path}") from exc

    parsed = yaml.safe_load("\n".join(lines[1:closing])) or {}
    if not isinstance(parsed, dict):
        raise SkillRuntimeError(f"SKILL.md frontmatter must be a mapping: {path}")
    return {str(key): value for key, value in parsed.items()}, "\n".join(lines[closing + 1 :])


def _content_hash(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _render_skill_context(skill: Skill, contents: str) -> str:
    source_path = f"{skill.skill_id}/{skill.entrypoint}"
    return (
        "<skill>\n"
        f"<name>{skill.name}</name>\n"
        f"<path>{source_path}</path>\n"
        f"{contents}\n"
        "</skill>"
    )


class SkillRuntime:
    """Discover Skill metadata, load selected entrypoints, and read referenced files."""

    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir).expanduser().resolve()
        if not self.skills_dir.is_dir():
            raise FileNotFoundError(f"Skills directory does not exist: {self.skills_dir}")
        self._skills = self._load_skills()

    def _load_skills(self) -> dict[str, Skill]:
        skills: dict[str, Skill] = {}
        manifests = sorted(
            path
            for path in self.skills_dir.rglob("SKILL.md")
            if not any(
                part.startswith(".") or part == "__pycache__"
                for part in path.relative_to(self.skills_dir).parts
            )
        )
        for manifest in manifests:
            text = manifest.read_text(encoding="utf-8", errors="replace")
            frontmatter, _ = _parse_frontmatter(text, manifest)
            name = str(frontmatter.get("name") or "").strip()
            description = str(frontmatter.get("description") or "").strip()
            if not name or not description:
                raise SkillRuntimeError(
                    f"SKILL.md requires frontmatter name and description: {manifest}"
                )
            if not _SKILL_ID_RE.fullmatch(name):
                raise SkillRuntimeError(f"Invalid Skill name {name!r}: {manifest}")
            if name in skills:
                raise SkillRuntimeError(f"Duplicate Skill name {name!r}")
            skills[name] = Skill(
                skill_id=name,
                root=manifest.parent.resolve(),
                name=name,
                description=description,
                content_hash=_content_hash(manifest),
            )
        return skills

    def list_skills(self) -> dict[str, Any]:
        return {
            "skills_dir": str(self.skills_dir),
            "skills": [
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "entrypoint": skill.entrypoint,
                    "content_hash": skill.content_hash,
                }
                for skill in self._skills.values()
            ],
        }

    def load_skills(self, skill_ids: list[str]) -> dict[str, Any]:
        loaded: list[dict[str, Any]] = []
        for skill_id in _unique(skill_ids):
            skill = self._get_skill(skill_id)
            manifest = skill.root / skill.entrypoint
            contents = manifest.read_text(encoding="utf-8", errors="replace")
            loaded.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "source_path": f"{skill.skill_id}/{skill.entrypoint}",
                    "content": _render_skill_context(skill, contents),
                    "content_hash": skill.content_hash,
                    "referenced_paths": self._referenced_paths(skill, contents),
                }
            )
        return {
            "skills": loaded,
            "loaded_skill_ids": [item["skill_id"] for item in loaded],
        }

    def read(
        self,
        skill_id: str,
        path: str,
        start_line: int = 1,
        max_lines: int = 2000,
    ) -> dict[str, Any]:
        skill = self._get_skill(skill_id)
        file_path = self._resolve_path(skill, path)
        if not file_path.is_file():
            raise SkillPathError(f"Skill file not found: {path}")

        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if start_line < 1 or (lines and start_line > len(lines)):
            raise SkillPathError(f"start_line exceeds file length: {start_line}")
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        end_line = start_line + len(selected) - 1 if selected else 0
        truncated = end_line < len(lines)
        return {
            "skill_id": skill.skill_id,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "content": "\n".join(selected),
            "content_hash": skill.content_hash if path == "SKILL.md" else _content_hash(file_path),
            "truncated": truncated,
            "next_start_line": end_line + 1 if truncated else None,
        }

    def _get_skill(self, skill_id: str) -> Skill:
        if not _SKILL_ID_RE.fullmatch(skill_id):
            raise SkillNotFoundError(f"Invalid skill_id: {skill_id!r}")
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill not found: {skill_id}") from exc

    @staticmethod
    def _resolve_path(skill: Skill, path: str) -> Path:
        if not path or path.startswith(("/", "\\")):
            raise SkillPathError(f"Unsafe skill path: {path!r}")
        candidate = (skill.root / path).resolve()
        try:
            candidate.relative_to(skill.root)
        except ValueError as exc:
            raise SkillPathError(f"Unsafe skill path: {path!r}") from exc
        return candidate

    def _referenced_paths(self, skill: Skill, text: str) -> list[str]:
        candidates = list(_BACKTICK_PATH_RE.findall(text))
        for target in _MARKDOWN_LINK_RE.findall(text):
            target = target.split("#", 1)[0].strip()
            if target and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", target):
                candidates.append(target)

        result: list[str] = []
        for candidate in _unique(candidates):
            try:
                path = self._resolve_path(skill, candidate)
            except SkillPathError:
                continue
            if path.is_file():
                result.append(candidate)
        return result
