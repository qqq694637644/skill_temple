"""Codex-style skill discovery and progressive disclosure for GPT Actions.

A skill is defined by one required ``SKILL.md`` file. Discovery exposes only its
frontmatter name and description. After selection, its entrypoint is returned within
the response budget; additional references are read explicitly by safe relative path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_@*.-]+")
_EXPLICIT_SKILL_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])[@$]([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![A-Za-z0-9_-])"
)
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_BACKTICK_PATH_RE = re.compile(
    r"`((?:docs|references|scripts|assets)/[^`\r\n]+)`"
)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_API_SYMBOL_RE = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*)(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*(?:_t|_[A-Z0-9]+)\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\(\)"
)

DEFAULT_MAX_SKILLS = 3
DEFAULT_MANIFEST_MAX_CHARS = 24_000
RETRIEVE_INSTRUCTIONS_MAX_CHARS = 60_000
SKILL_CATALOG_MAX_CHARS = 20_000
DOTENV_FILE_NAME = ".env"
MAX_SKILL_SCAN_DEPTH = 6
SKILL_NAME_MAX_CHARS = 64
SKILL_DESCRIPTION_MAX_CHARS = 1024
_TEXT_REFERENCE_SUFFIXES = {".md", ".rst", ".txt"}


class SkillRuntimeError(RuntimeError):
    """Base error for skill runtime failures."""


class SkillNotFoundError(SkillRuntimeError):
    """Raised when a requested skill id is unavailable."""


class SkillPathError(SkillRuntimeError):
    """Raised when a requested skill path is invalid or unsafe."""


@dataclass(frozen=True)
class Skill:
    """A discovered SKILL.md entrypoint."""

    skill_id: str
    root: Path
    name: str
    description: str
    content_hash: str

    @property
    def entrypoint(self) -> str:
        return "SKILL.md"


def load_runtime(skills_dir: str | Path | None = None) -> SkillRuntime:
    """Create a runtime from an explicit path, environment, cwd, or packaged examples."""

    return SkillRuntime(_resolve_skills_dir(skills_dir))


def _resolve_skills_dir(skills_dir: str | Path | None) -> Path:
    if skills_dir:
        return Path(skills_dir).expanduser().resolve()

    env_value = env_value_from_environment_or_dotenv("SKILL_TEMPLE_SKILLS_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()

    cwd_skills = Path.cwd() / "skills"
    if cwd_skills.exists():
        return cwd_skills.resolve()

    with resources.as_file(resources.files("skill_temple") / "example_skills") as path:
        return path.resolve()


def env_value_from_environment_or_dotenv(name: str) -> str | None:
    """Return an environment value, falling back to the current directory .env file."""

    value = os.environ.get(name)
    if value:
        return value
    return _read_dotenv_file(Path.cwd() / DOTENV_FILE_NAME).get(name)


def _read_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(line)
        if parsed is not None:
            key, value = parsed
            values[key] = value
    return values


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not _ENV_KEY_RE.fullmatch(key):
        return None

    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return key, value[1:-1]
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return key, value


def _safe_skill_id(skill_id: str) -> str:
    if not _SKILL_ID_RE.fullmatch(skill_id):
        raise SkillNotFoundError(f"Invalid skill_id: {skill_id!r}")
    return skill_id


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text)]


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _content_hash(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _json_char_count(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _explicit_skill_mentions(query: str) -> list[str]:
    return _unique_preserve_order(
        [match.group(1) for match in _EXPLICIT_SKILL_MENTION_RE.finditer(query)]
    )


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the YAML frontmatter used by Codex-style ``SKILL.md`` files."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillRuntimeError("SKILL.md is missing YAML frontmatter")

    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillRuntimeError("SKILL.md frontmatter is not closed with ---") from exc

    parsed = yaml.safe_load("\n".join(lines[1:closing])) or {}
    if not isinstance(parsed, dict):
        raise SkillRuntimeError("SKILL.md frontmatter must be a YAML mapping")
    data = {str(key): value for key, value in parsed.items()}
    body = "\n".join(lines[closing + 1 :])
    return data, body


class SkillRuntime:
    """Discover SKILL.md files and expose progressive disclosure operations."""

    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir).expanduser().resolve()
        if not self.skills_dir.exists():
            raise FileNotFoundError(f"Skills directory does not exist: {self.skills_dir}")
        if not self.skills_dir.is_dir():
            raise NotADirectoryError(f"Skills path is not a directory: {self.skills_dir}")
        self._skills = self._load_skills()
        self._search_lock = threading.RLock()
        self._search_db = sqlite3.connect(":memory:", check_same_thread=False)
        self._search_db.row_factory = sqlite3.Row
        self._build_search_index()

    def _load_skills(self) -> dict[str, Skill]:
        skills: dict[str, Skill] = {}
        for manifest_path in self._iter_skill_manifests():
            skill = self._load_skill(manifest_path)
            if skill.skill_id in skills:
                other = skills[skill.skill_id].root
                raise SkillRuntimeError(
                    f"Duplicate skill name {skill.skill_id!r}: {other} and {skill.root}"
                )
            skills[skill.skill_id] = skill
        return skills

    def _iter_skill_manifests(self) -> list[Path]:
        manifests: list[Path] = []
        for current_root, dir_names, file_names in os.walk(self.skills_dir, topdown=True):
            current_path = Path(current_root)
            depth = len(current_path.relative_to(self.skills_dir).parts)
            dir_names[:] = sorted(
                name
                for name in dir_names
                if not name.startswith(".") and name != "__pycache__"
            )
            if depth >= MAX_SKILL_SCAN_DEPTH:
                dir_names[:] = []
            if "SKILL.md" in file_names:
                manifests.append(current_path / "SKILL.md")
        return sorted(manifests)

    def _load_skill(self, manifest_path: Path) -> Skill:
        text = manifest_path.read_text(encoding="utf-8", errors="replace")
        frontmatter, _body = _parse_frontmatter(text)
        name = str(frontmatter.get("name") or "").strip()
        description = str(frontmatter.get("description") or "").strip()
        if not name:
            raise SkillRuntimeError(f"SKILL.md is missing frontmatter name: {manifest_path}")
        if not description:
            raise SkillRuntimeError(f"SKILL.md is missing frontmatter description: {manifest_path}")
        if len(name) > SKILL_NAME_MAX_CHARS:
            raise SkillRuntimeError(
                f"SKILL.md frontmatter name exceeds {SKILL_NAME_MAX_CHARS} characters: "
                f"{manifest_path}"
            )
        if len(description) > SKILL_DESCRIPTION_MAX_CHARS:
            raise SkillRuntimeError(
                "SKILL.md frontmatter description exceeds "
                f"{SKILL_DESCRIPTION_MAX_CHARS} characters: {manifest_path}"
            )
        skill_id = _safe_skill_id(name)
        return Skill(
            skill_id=skill_id,
            root=manifest_path.parent.resolve(),
            name=name,
            description=description,
            content_hash=_content_hash(manifest_path),
        )

    def list_skills(self) -> dict[str, Any]:
        """Return discovery metadata only; skill bodies remain undisclosed."""

        return {
            "skills_dir": str(self.skills_dir),
            "skills": [self._public_skill_metadata(skill) for skill in self._skills.values()],
        }

    def _catalog_metadata(self, *, include_catalog: bool) -> dict[str, Any]:
        total_count = len(self._skills)
        if not include_catalog:
            return {
                "available_skills": [],
                "available_skill_count": total_count,
                "included_skill_count": 0,
                "omitted_skill_count": total_count,
                "descriptions_truncated": False,
                "catalog_char_limit": SKILL_CATALOG_MAX_CHARS,
                "catalog_included": False,
            }

        available_skills: list[dict[str, Any]] = []
        descriptions_truncated = False
        for skill in self._skills.values():
            metadata = self._public_skill_metadata(skill)
            if _json_char_count([*available_skills, metadata]) <= SKILL_CATALOG_MAX_CHARS:
                available_skills.append(metadata)
                continue

            description = skill.description
            low = 0
            high = len(description)
            best: dict[str, Any] | None = None
            while low <= high:
                middle = (low + high) // 2
                candidate = dict(metadata)
                candidate["description"] = description[:middle].rstrip() + "..."
                candidate["description_truncated"] = True
                if (
                    _json_char_count([*available_skills, candidate])
                    <= SKILL_CATALOG_MAX_CHARS
                ):
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1

            if best is not None:
                available_skills.append(best)
                descriptions_truncated = True
            break

        included_count = len(available_skills)
        return {
            "available_skills": available_skills,
            "available_skill_count": total_count,
            "included_skill_count": included_count,
            "omitted_skill_count": total_count - included_count,
            "descriptions_truncated": descriptions_truncated,
            "catalog_char_limit": SKILL_CATALOG_MAX_CHARS,
            "catalog_included": True,
        }

    def resolve(
        self,
        query: str,
        hinted_skill_ids: list[str] | None = None,
        max_results: int = 3,
        include_catalog: bool = True,
    ) -> dict[str, Any]:
        """Resolve only explicit hints or exact ``@/$skill`` mentions.

        Codex exposes the skill catalog to the model and lets the model decide whether a
        description clearly matches the task. The runtime does not reproduce that semantic
        judgment with server-side keyword scoring.
        """

        hinted_skill_ids = _unique_preserve_order(hinted_skill_ids or [])
        for skill_id in hinted_skill_ids:
            self._get_skill(skill_id)

        mentioned_skill_ids = _explicit_skill_mentions(query)
        known_mentions = [
            skill_id for skill_id in mentioned_skill_ids if skill_id in self._skills
        ]
        unknown_mentions = [
            skill_id for skill_id in mentioned_skill_ids if skill_id not in self._skills
        ]
        selected_ids = _unique_preserve_order([*hinted_skill_ids, *known_mentions])
        matches: list[dict[str, Any]] = []
        for index, skill_id in enumerate(selected_ids):
            skill = self._skills.get(skill_id)
            if skill is None:
                continue
            hinted = skill_id in hinted_skill_ids
            matches.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "selection_order": index,
                    "reason": "explicit skill hint" if hinted else "exact @/$ skill mention",
                    "recommended_next_call": "retrieveSkillContext",
                }
            )

        result = {
            "matches": matches[:max_results],
            "explicit_skill_ids": selected_ids,
            "unknown_skill_mentions": unknown_mentions,
        }
        result.update(self._catalog_metadata(include_catalog=include_catalog))
        return result

    def retrieve(
        self,
        query: str,
        hinted_skill_ids: list[str] | None = None,
        max_skills: int = DEFAULT_MAX_SKILLS,
        allow_skill_chaining: bool = False,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        """Load explicit skills and return each entrypoint within the response budget."""

        effective_max = min(DEFAULT_MAX_SKILLS, max(1, max_skills))
        resolved = self.resolve(
            query,
            hinted_skill_ids=hinted_skill_ids,
            max_results=max(len(self._skills), effective_max + 1),
            include_catalog=False,
        )
        explicit_skill_ids = resolved["explicit_skill_ids"]
        unknown_skill_mentions = resolved["unknown_skill_mentions"]
        omitted_explicit_skill_ids = explicit_skill_ids[effective_max:]
        selected_matches = [] if omitted_explicit_skill_ids else resolved["matches"]
        selected: list[dict[str, Any]] = []
        any_truncated = False
        remaining_instruction_chars = RETRIEVE_INSTRUCTIONS_MAX_CHARS
        used_instruction_chars = 0

        for index, match in enumerate(selected_matches):
            skill = self._get_skill(match["skill_id"])
            remaining_skill_count = len(selected_matches) - index
            skill_instruction_budget = min(
                DEFAULT_MANIFEST_MAX_CHARS,
                max(1, remaining_instruction_chars // remaining_skill_count),
            )
            manifest = self.read(
                skill.skill_id,
                skill.entrypoint,
                start_line=1,
                max_lines=5000,
                max_chars=skill_instruction_budget,
            )
            instruction_chars = len(manifest["content"])
            remaining_instruction_chars -= instruction_chars
            used_instruction_chars += instruction_chars
            any_truncated = any_truncated or manifest["truncated"]
            packet: dict[str, Any] = {
                "skill_id": skill.skill_id,
                "name": skill.name,
                "description": skill.description,
                "role": "primary" if index == 0 else "secondary",
                "source_path": skill.entrypoint,
                "instructions": manifest["content"],
                "content_hash": manifest["content_hash"],
                "total_lines": manifest["total_lines"],
                "truncated": manifest["truncated"],
                "next_start_line": manifest["next_start_line"],
                "referenced_paths": self._referenced_paths(skill, manifest["content"]),
            }
            if include_debug:
                packet["debug"] = {
                    "why_selected": match["reason"],
                    "selection_order": match["selection_order"],
                    "skill_root": str(skill.root),
                }
            selected.append(packet)

        if omitted_explicit_skill_ids:
            decision = {
                "selected": False,
                "next_action": "retryWithFewerSkills",
                "reason": (
                    f"{len(explicit_skill_ids)} skills were explicitly selected, but at most "
                    f"{effective_max} can be loaded in one response. Retry with a smaller set."
                ),
                "stop_retrieval": False,
            }
        elif not selected:
            if self._skills:
                if unknown_skill_mentions:
                    reason = (
                        "Explicitly mentioned skills are unavailable: "
                        + ", ".join(unknown_skill_mentions)
                        + ". Review available_skills, correct the name, or continue without it."
                    )
                else:
                    reason = (
                        "No explicit skill was selected. Review available_skills; retry once "
                        "with exact hinted_skill_ids only when a description clearly matches."
                    )
                decision = {
                    "selected": False,
                    "next_action": "selectSkillOrAnswer",
                    "reason": reason,
                    "stop_retrieval": False,
                }
            else:
                decision = {
                    "selected": False,
                    "next_action": "answerWithoutSkill",
                    "reason": "No skills are available.",
                    "stop_retrieval": True,
                }
        elif any_truncated:
            reason = "A selected SKILL.md was truncated; continue from next_start_line."
            if unknown_skill_mentions:
                reason += " Unavailable explicit mentions: " + ", ".join(
                    unknown_skill_mentions
                )
            decision = {
                "selected": True,
                "next_action": "readSkillContent",
                "reason": reason,
                "stop_retrieval": False,
            }
        else:
            reason = (
                "Read each returned SKILL.md completely, then read only the references "
                "it directly identifies for this task."
            )
            if unknown_skill_mentions:
                reason += " Unavailable explicit mentions: " + ", ".join(
                    unknown_skill_mentions
                )
            decision = {
                "selected": True,
                "next_action": "followSkillInstructions",
                "reason": reason,
                "stop_retrieval": True,
            }

        result: dict[str, Any] = {
            "selected_skills": selected,
            "explicit_skill_ids": explicit_skill_ids,
            "unknown_skill_mentions": unknown_skill_mentions,
            "omitted_explicit_skill_ids": omitted_explicit_skill_ids,
            "decision": decision,
        }
        result.update(self._catalog_metadata(include_catalog=not selected))
        if include_debug:
            result["debug"] = {
                "available_skill_count": len(self._skills),
                "allow_skill_chaining_requested": allow_skill_chaining,
                "automatic_skill_chaining": len(selected_matches) > 1,
                "instruction_char_limit": RETRIEVE_INSTRUCTIONS_MAX_CHARS,
                "used_instruction_chars": used_instruction_chars,
                "resolved_matches": resolved["matches"],
            }
        return result

    def search(
        self,
        skill_id: str,
        query: str,
        paths: list[str] | None = None,
        limit: int = 5,
        mode: str = "keyword",
        max_chars_per_match: int = 2000,
        include_manifest: bool = False,
    ) -> dict[str, Any]:
        """Find candidate reference paths when SKILL.md does not provide an exact route."""

        if mode != "keyword":
            raise SkillRuntimeError("Only keyword search mode is currently supported")

        skill = self._get_skill(skill_id)
        allowed_paths: set[str] | None = None
        if paths:
            allowed_paths = set()
            for rel_path in paths:
                file_path = self._resolve_path(skill, rel_path)
                if not file_path.exists() or not file_path.is_file():
                    raise SkillPathError(f"Skill file not found: {rel_path}")
                allowed_paths.add(rel_path)

        matches = self._search_keyword(
            skill=skill,
            query=query,
            allowed_paths=allowed_paths,
            limit=limit,
            max_chars_per_match=max_chars_per_match,
            include_manifest=include_manifest,
        )
        return {
            "skill_id": skill.skill_id,
            "query": query,
            "mode": "keyword",
            "engine": "sqlite_fts5_symbol_index",
            "matches": matches,
            "recommended_next_action": "readSkillContent" if matches else "none",
        }

    def read(
        self,
        skill_id: str,
        path: str,
        start_line: int = 1,
        max_lines: int = 2000,
        max_chars: int = 32_000,
    ) -> dict[str, Any]:
        """Read an exact skill-relative path with continuation information."""

        skill = self._get_skill(skill_id)
        file_path = self._resolve_path(skill, path)
        if not file_path.exists() or not file_path.is_file():
            raise SkillPathError(f"Skill file not found: {path}")

        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        if start > max(1, len(lines)):
            raise SkillPathError(f"start_line exceeds file length: {start_line}")

        selected: list[str] = []
        end = start - 1
        char_count = 0
        max_end = min(len(lines), start + max_lines - 1)
        for line_number in range(start, max_end + 1):
            line = lines[line_number - 1]
            added = len(line) + (1 if selected else 0)
            if selected and char_count + added > max_chars:
                break
            if not selected and len(line) > max_chars:
                selected.append(line)
                char_count = len(line)
                end = line_number
                break
            selected.append(line)
            char_count += added
            end = line_number

        truncated = end < len(lines)
        next_start_line = end + 1 if truncated else None
        return {
            "skill_id": skill.skill_id,
            "path": path,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "content": "\n".join(selected),
            "content_hash": (
                skill.content_hash
                if path == skill.entrypoint
                else _content_hash(file_path)
            ),
            "truncated": truncated,
            "next_start_line": next_start_line,
        }

    def _get_skill(self, skill_id: str) -> Skill:
        skill_id = _safe_skill_id(skill_id)
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill not found: {skill_id}") from exc

    def _resolve_path(self, skill: Skill, path: str) -> Path:
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
        for candidate in _unique_preserve_order(candidates):
            if "<" in candidate or ">" in candidate:
                continue
            try:
                path = self._resolve_path(skill, candidate)
            except SkillPathError:
                continue
            if path.exists() and path.is_file():
                result.append(candidate)
        return result

    def _candidate_paths(self, skill: Skill, include_manifest: bool) -> list[str]:
        candidates: list[str] = [skill.entrypoint] if include_manifest else []
        for file_path in sorted(skill.root.rglob("*")):
            if not file_path.is_file() or file_path.name == skill.entrypoint:
                continue
            if file_path.suffix.lower() not in _TEXT_REFERENCE_SUFFIXES:
                continue
            relative = file_path.relative_to(skill.root)
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            candidates.append(relative.as_posix())
        return candidates

    def _build_search_index(self) -> None:
        with self._search_lock:
            try:
                self._search_db.execute(
                    """
                    CREATE VIRTUAL TABLE skill_docs_fts USING fts5(
                        skill_id,
                        path,
                        title,
                        heading_path,
                        content,
                        symbols,
                        start_line UNINDEXED,
                        end_line UNINDEXED,
                        doc_kind UNINDEXED,
                        priority UNINDEXED,
                        content_hash UNINDEXED
                    )
                    """
                )
            except sqlite3.OperationalError as exc:  # pragma: no cover
                raise SkillRuntimeError("SQLite FTS5 support is required") from exc

            for skill in self._skills.values():
                for chunk in self._iter_search_chunks(skill):
                    self._search_db.execute(
                        """
                        INSERT INTO skill_docs_fts(
                            skill_id, path, title, heading_path, content, symbols,
                            start_line, end_line, doc_kind, priority, content_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            skill.skill_id,
                            chunk["path"],
                            chunk["title"],
                            chunk["heading_path"],
                            chunk["content"],
                            " ".join(chunk["symbols"]),
                            chunk["start_line"],
                            chunk["end_line"],
                            chunk["doc_kind"],
                            chunk["priority"],
                            chunk["content_hash"],
                        ),
                    )
            self._search_db.commit()

    def _iter_search_chunks(self, skill: Skill) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for rel_path in self._candidate_paths(skill, include_manifest=True):
            file_path = self._resolve_path(skill, rel_path)
            text = file_path.read_text(encoding="utf-8", errors="replace")
            chunks.extend(self._chunk_file(skill, rel_path, text))
        return chunks

    def _chunk_file(self, skill: Skill, rel_path: str, text: str) -> list[dict[str, Any]]:
        lines = text.splitlines()
        heading_indices = [index for index, line in enumerate(lines) if _HEADING_RE.match(line)]
        if not heading_indices:
            heading_indices = [0]

        chunks: list[dict[str, Any]] = []
        for position, start_index in enumerate(heading_indices):
            end_index = (
                heading_indices[position + 1]
                if position + 1 < len(heading_indices)
                else len(lines)
            )
            content = "\n".join(lines[start_index:end_index]).strip()
            if not content:
                continue
            title = self._chunk_title(lines[start_index:end_index], rel_path)
            chunks.append(
                {
                    "path": rel_path,
                    "title": title,
                    "heading_path": title,
                    "content": content,
                    "symbols": self._extract_symbols(f"{rel_path}\n{title}\n{content}"),
                    "start_line": start_index + 1,
                    "end_line": end_index,
                    "doc_kind": self._doc_kind(skill, rel_path),
                    "priority": self._doc_priority(skill, rel_path),
                    "content_hash": _content_hash(self._resolve_path(skill, rel_path)),
                }
            )
        return chunks

    def _chunk_title(self, lines: list[str], rel_path: str) -> str:
        for line in lines[:5]:
            match = _HEADING_RE.match(line)
            if match:
                return match.group(2).strip()
        return Path(rel_path).stem

    def _doc_kind(self, skill: Skill, rel_path: str) -> str:
        if rel_path == skill.entrypoint:
            return "manifest"
        if rel_path.endswith(".rst"):
            return "full_reference"
        return "reference"

    def _doc_priority(self, skill: Skill, rel_path: str) -> float:
        kind = self._doc_kind(skill, rel_path)
        if kind == "manifest":
            return 50.0
        if kind == "reference":
            return 20.0
        return 5.0

    def _extract_symbols(self, text: str) -> list[str]:
        symbols: list[str] = []
        for match in _BACKTICK_RE.findall(text):
            symbols.extend(_API_SYMBOL_RE.findall(match))
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", match):
                symbols.append(match)
        symbols.extend(_API_SYMBOL_RE.findall(text))
        normalized: list[str] = []
        for symbol in symbols:
            clean = symbol.strip().strip("`.,:;()")
            if len(clean) >= 3:
                normalized.append(clean)
                if "." in clean:
                    normalized.extend(part for part in clean.split(".") if len(part) >= 3)
        return _unique_preserve_order(normalized)

    def _fts_query(self, query: str) -> str:
        terms = [term.lower() for term in _FTS_TOKEN_RE.findall(query) if len(term) >= 2]
        return " OR ".join(f'"{term}"' for term in _unique_preserve_order(terms)[:16])

    def _search_keyword(
        self,
        skill: Skill,
        query: str,
        allowed_paths: set[str] | None,
        limit: int,
        max_chars_per_match: int,
        include_manifest: bool,
    ) -> list[dict[str, Any]]:
        match_query = self._fts_query(query)
        if not match_query:
            return []

        with self._search_lock:
            rows = self._search_db.execute(
                """
                SELECT rowid, skill_id, path, title, heading_path, content, symbols,
                       start_line, end_line, doc_kind, priority, content_hash,
                       bm25(skill_docs_fts) AS bm25_rank
                FROM skill_docs_fts
                WHERE skill_docs_fts MATCH ? AND skill_id = ?
                ORDER BY bm25_rank
                LIMIT 200
                """,
                (match_query, skill.skill_id),
            ).fetchall()

        query_terms = set(_tokens(query))
        query_symbols = set(self._extract_symbols(query))
        scored: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()
        for rank_index, row in enumerate(rows):
            rel_path = str(row["path"])
            if allowed_paths is not None and rel_path not in allowed_paths:
                continue
            if not include_manifest and row["doc_kind"] == "manifest":
                continue
            key = (rel_path, int(row["start_line"]), int(row["end_line"]))
            if key in seen:
                continue
            seen.add(key)

            row_symbols = set(str(row["symbols"] or "").split())
            heading_tokens = set(_tokens(str(row["heading_path"] or "")))
            path_tokens = set(_tokens(rel_path))
            symbol_overlap = query_symbols & row_symbols
            heading_overlap = query_terms & heading_tokens
            path_overlap = query_terms & path_tokens
            fts_rank_score = 50.0 / (rank_index + 1)
            score = (
                fts_rank_score
                + 100.0 * len(symbol_overlap)
                + 40.0 * len(path_overlap)
                + 30.0 * len(heading_overlap)
                + float(row["priority"] or 0.0)
            )
            rank_features = {
                "symbol_matches": sorted(symbol_overlap),
                "document_symbols": sorted(row_symbols)[:25],
                "path_matches": sorted(path_overlap),
                "heading_matches": sorted(heading_overlap),
                "fts_rank": float(row["bm25_rank"] or 0.0),
                "doc_priority": float(row["priority"] or 0.0),
            }
            scored.append(
                {
                    "skill_id": skill.skill_id,
                    "path": rel_path,
                    "title": str(row["title"] or Path(rel_path).stem),
                    "heading_path": str(row["heading_path"] or ""),
                    "score": round(score, 4),
                    "mode": "keyword",
                    "engine": "sqlite_fts5_symbol_index",
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                    "excerpt": str(row["content"] or "")[:max_chars_per_match],
                    "symbols": sorted(symbol_overlap),
                    "document_symbols": sorted(row_symbols)[:25],
                    "rank_features": rank_features,
                    "why_relevant": self._why_relevant(rank_features),
                    "content_hash": str(row["content_hash"]),
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def _why_relevant(self, rank_features: dict[str, Any]) -> str:
        if rank_features.get("symbol_matches"):
            return "Matched exact API or symbol names."
        if rank_features.get("path_matches"):
            return "Matched path or module terms."
        if rank_features.get("heading_matches"):
            return "Matched section heading terms."
        return "Matched full-text keyword search."

    def _public_skill_metadata(self, skill: Skill) -> dict[str, Any]:
        return {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "description_truncated": False,
            "entrypoint": skill.entrypoint,
            "content_hash": skill.content_hash,
        }
