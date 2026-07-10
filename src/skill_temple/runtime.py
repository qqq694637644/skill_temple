"""Core Skill Runtime retrieval logic.

The core module intentionally has no web-framework dependency. It can be tested
and embedded independently, while ``skill_temple.app`` exposes it as a FastAPI
server suitable for GPT Actions.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_@*.-]+")
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_API_SYMBOL_RE = re.compile(
    r"\b(?:ida_[A-Za-z0-9_]+|idautils|idaapi|idc)(?:\.[A-Za-z_][A-Za-z0-9_]*)?\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*(?:_t|_[A-Z0-9]+)\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\(\)"
)

DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_DOCS = 6
DEFAULT_MAX_SKILLS = 1
DOTENV_FILE_NAME = ".env"


class SkillRuntimeError(RuntimeError):
    """Base error for skill runtime failures."""


class SkillNotFoundError(SkillRuntimeError):
    """Raised when a requested skill id is unavailable."""


class SkillPathError(SkillRuntimeError):
    """Raised when a requested skill path is invalid or unsafe."""


@dataclass(frozen=True)
class Skill:
    """Loaded skill metadata and root path."""

    skill_id: str
    root: Path
    metadata: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.metadata.get("name") or self.skill_id)

    @property
    def description(self) -> str:
        return str(self.metadata.get("description") or "")

    @property
    def version(self) -> str:
        return str(self.metadata.get("version") or "0")

    @property
    def entrypoint(self) -> str:
        return str(self.metadata.get("entrypoint") or "SKILL.md")

    @property
    def aliases(self) -> list[str]:
        return [str(item) for item in self.metadata.get("aliases", [])]

    @property
    def trigger_terms(self) -> list[str]:
        activation = self.metadata.get("activation") or {}
        terms = activation.get("trigger_terms") or self.metadata.get("trigger_terms") or []
        return [str(item) for item in terms]

    @property
    def policy(self) -> dict[str, Any]:
        return dict(self.metadata.get("policy") or {})

    @property
    def retrieval(self) -> dict[str, Any]:
        return dict(self.metadata.get("retrieval") or {})

    @property
    def skill_type(self) -> str:
        return str(self.metadata.get("skill_type") or "tool_doc")

    @property
    def capability_tags(self) -> list[str]:
        return [str(item) for item in self.metadata.get("capability_tags", [])]

    @property
    def domains(self) -> list[str]:
        return [str(item) for item in self.metadata.get("domains", [])]

    @property
    def conflicts_with(self) -> list[str]:
        return [str(item) for item in self.metadata.get("conflicts_with", [])]

    @property
    def can_chain_with(self) -> list[str]:
        return [str(item) for item in self.metadata.get("can_chain_with", [])]


def load_runtime(skills_dir: str | Path | None = None) -> SkillRuntime:
    """Create a runtime from an explicit path, environment, cwd, or packaged examples."""

    selected = _resolve_skills_dir(skills_dir)
    return SkillRuntime(selected)


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
        if parsed is None:
            continue
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


def _read_text(path: Path, max_chars: int | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars]
    return text


def _content_hash(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse a small YAML-like frontmatter block without external dependencies."""

    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    block = text[4:end].strip()
    body = text[text.find("\n", end + 1) + 1 :]
    data: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"\'')
    return data, body


def _section_lines(markdown: str, heading: str) -> list[str]:
    wanted = heading.strip().lower()
    lines = markdown.splitlines()
    start: int | None = None
    start_level = 0
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip().lower()
        if title == wanted:
            start = index + 1
            start_level = level
            break
    if start is None:
        return []

    end = len(lines)
    for index in range(start, len(lines)):
        match = _HEADING_RE.match(lines[index])
        if match and len(match.group(1)) <= start_level:
            end = index
            break
    return lines[start:end]


def _extract_bullets(lines: list[str], limit: int = 10) -> list[str]:
    bullets: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(('-', '*')):
            bullets.append(stripped[1:].strip())
        elif re.match(r"^\d+[.)]\s+", stripped):
            bullets.append(re.sub(r"^\d+[.)]\s+", "", stripped).strip())
        elif bullets and not stripped.startswith("|"):
            bullets[-1] = f"{bullets[-1]} {stripped}"
        if len(bullets) >= limit:
            break
    return bullets


def _extract_markdown_table(lines: list[str], max_rows: int = 20) -> list[dict[str, str]]:
    table_lines = [line.strip() for line in lines if line.strip().startswith("|")]
    if len(table_lines) < 2:
        return []
    header = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells, strict=True)))
        if len(rows) >= max_rows:
            break
    return rows


class SkillRuntime:
    """Local registry, search, and retrieval service for reusable skills."""

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
        for root in sorted(self.skills_dir.iterdir()):
            if not root.is_dir():
                continue
            skill = self._load_skill(root)
            skills[skill.skill_id] = skill
        return skills

    def _load_skill(self, root: Path) -> Skill:
        metadata_path = root / "skill.json"
        manifest_path = root / "SKILL.md"
        metadata: dict[str, Any]
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        elif manifest_path.exists():
            frontmatter, _body = _parse_frontmatter(manifest_path.read_text(encoding="utf-8"))
            metadata = dict(frontmatter)
        else:
            raise SkillRuntimeError(f"Skill directory lacks skill.json or SKILL.md: {root}")

        skill_id = str(metadata.get("skill_id") or metadata.get("name") or root.name)
        _safe_skill_id(skill_id)
        metadata.setdefault("skill_id", skill_id)
        metadata.setdefault("name", skill_id)
        metadata.setdefault("entrypoint", "SKILL.md")
        metadata.setdefault("aliases", [f"@{skill_id}", skill_id])
        return Skill(skill_id=skill_id, root=root.resolve(), metadata=metadata)

    def list_skills(self) -> dict[str, Any]:
        """Return public metadata for every loaded skill."""

        return {
            "skills_dir": str(self.skills_dir),
            "skills": [self._public_skill_metadata(skill) for skill in self._skills.values()],
        }

    def resolve(
        self,
        query: str,
        hinted_skill_ids: list[str] | None = None,
        max_results: int = 3,
    ) -> dict[str, Any]:
        """Rank available skills for a user task."""

        hinted_skill_ids = hinted_skill_ids or []
        query_tokens = set(_tokens(query))
        query_lower = query.lower()
        matches: list[dict[str, Any]] = []

        for skill in self._skills.values():
            score = 0.0
            reasons: list[str] = []
            if skill.skill_id in hinted_skill_ids:
                score += 8.0
                reasons.append("explicit skill hint")

            for alias in skill.aliases:
                alias_lower = alias.lower()
                if alias_lower and alias_lower in query_lower:
                    score += 6.0
                    reasons.append(f"matched alias {alias!r}")

            for term in skill.trigger_terms:
                term_lower = term.lower()
                if term_lower and term_lower in query_lower:
                    score += 3.0
                    reasons.append(f"matched trigger term {term!r}")

            metadata_tokens = set(
                _tokens(" ".join([skill.name, skill.description, *skill.aliases]))
            )
            overlap = query_tokens & metadata_tokens
            if overlap:
                score += min(5.0, len(overlap) * 0.75)
                reasons.append("metadata token overlap")

            if score <= 0:
                continue

            confidence = min(0.99, score / 12.0)
            matches.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "confidence": round(confidence, 3),
                    "score": round(score, 3),
                    "reason": "; ".join(_unique_preserve_order(reasons)),
                    "recommended_next_call": "retrieveSkillContext",
                }
            )

        matches.sort(key=lambda item: item["score"], reverse=True)
        return {"matches": matches[:max_results]}

    def _validate_hinted_skill_ids(self, hinted_skill_ids: list[str] | None) -> None:
        for skill_id in hinted_skill_ids or []:
            self._get_skill(skill_id)

    def _select_chainable_matches(
        self,
        matches: list[dict[str, Any]],
        max_skills: int,
        allow_skill_chaining: bool,
    ) -> list[dict[str, Any]]:
        if not matches or max_skills <= 0:
            return []
        if not allow_skill_chaining:
            return matches[:1]

        selected: list[dict[str, Any]] = []
        for match in matches:
            candidate = self._get_skill(match["skill_id"])
            if selected and not self._can_add_to_chain(candidate, selected):
                continue
            selected.append(match)
            if len(selected) >= max_skills:
                break
        return selected

    def _can_add_to_chain(self, candidate: Skill, selected_matches: list[dict[str, Any]]) -> bool:
        for selected_match in selected_matches:
            selected_skill = self._get_skill(selected_match["skill_id"])
            if self._skills_conflict(selected_skill, candidate):
                return False
            if not self._skills_can_chain(selected_skill, candidate):
                return False
        return True

    def _skills_conflict(self, first: Skill, second: Skill) -> bool:
        return second.skill_id in first.conflicts_with or first.skill_id in second.conflicts_with

    def _skills_can_chain(self, first: Skill, second: Skill) -> bool:
        first_allows = not first.can_chain_with or second.skill_id in first.can_chain_with
        second_allows = not second.can_chain_with or first.skill_id in second.can_chain_with
        return first_allows and second_allows

    def retrieve(
        self,
        query: str,
        hinted_skill_ids: list[str] | None = None,
        max_skills: int = DEFAULT_MAX_SKILLS,
        max_docs: int = DEFAULT_MAX_DOCS,
        max_chars: int = DEFAULT_MAX_CHARS,
        include_manifest: bool = True,
        include_policy: bool = True,
        include_recommended_tools: bool = True,
        allow_skill_chaining: bool = False,
        include_debug: bool = False,
    ) -> dict[str, Any]:
        """Retrieve sufficient skill context for a user task in one call."""

        self._validate_hinted_skill_ids(hinted_skill_ids)
        effective_max_skills = max_skills if allow_skill_chaining else 1
        resolved = self.resolve(
            query,
            hinted_skill_ids=hinted_skill_ids,
            max_results=max(len(self._skills), effective_max_skills),
        )
        selected_matches = self._select_chainable_matches(
            resolved["matches"],
            max_skills=effective_max_skills,
            allow_skill_chaining=allow_skill_chaining,
        )
        selected: list[dict[str, Any]] = []
        not_ready_skill_ids: list[str] = []
        budget_remaining = max_chars
        used_chars = 0
        used_docs = 0
        truncated = False

        for index, match in enumerate(selected_matches):
            skill = self._get_skill(match["skill_id"])
            manifest_text = self._read_skill_file(skill, skill.entrypoint, max_chars=6000)
            manifest_summary = self._manifest_summary(manifest_text) if include_manifest else {}
            per_doc_budget = max(1000, budget_remaining // max(1, max_docs))
            search_result = self.search(
                skill_id=skill.skill_id,
                query=query,
                limit=max_docs,
                max_chars_per_match=per_doc_budget,
                include_manifest=False,
            )

            docs: list[dict[str, Any]] = []
            for doc in search_result["matches"]:
                content_len = len(doc.get("excerpt", ""))
                if content_len > budget_remaining:
                    truncated = True
                    break
                budget_remaining -= content_len
                used_chars += content_len
                used_docs += 1
                docs.append(doc)

            role = "primary" if index == 0 else "secondary"
            operating_rules = self._operating_rules(manifest_summary, skill)
            response_contract = self._response_contract(skill, manifest_summary, docs)
            answer_readiness = self._answer_readiness(skill, docs, truncated)
            if not answer_readiness["ready"]:
                not_ready_skill_ids.append(skill.skill_id)
            evidence = self._evidence(docs, include_debug=include_debug)
            selected_packet: dict[str, Any] = {
                "skill_id": skill.skill_id,
                "role": role,
                "confidence": match["confidence"],
                "capability_tags": skill.capability_tags[:6],
                "operating_rules": operating_rules,
                "response_contract": response_contract,
                "evidence": evidence,
                "validation_guidance": self._validation_guidance(skill),
            }
            if include_debug:
                selected_packet["debug"] = {
                    "name": skill.name,
                    "version": skill.version,
                    "skill_type": skill.skill_type,
                    "domains": skill.domains,
                    "conflicts_with": skill.conflicts_with,
                    "can_chain_with": skill.can_chain_with,
                    "why_selected": match["reason"],
                    "activation": {
                        "confidence": match["confidence"],
                        "reason": match["reason"],
                        "hinted": skill.skill_id in (hinted_skill_ids or []),
                    },
                    "manifest_hash": self._hash_if_exists(skill, skill.entrypoint),
                    "manifest_summary": manifest_summary,
                    "retrieved_docs": docs,
                    "answer_readiness": answer_readiness,
                    "tool_policy": skill.policy if include_policy else {},
                    "recommended_tools": (
                        self._recommended_tools(skill) if include_recommended_tools else []
                    ),
                    "execution_guidance": self._execution_guidance(skill, manifest_summary, docs),
                }
            selected.append(selected_packet)

        ready = bool(selected) and not truncated and not not_ready_skill_ids
        stop_reason = self._stop_reason(selected, truncated, not_ready_skill_ids)
        decision = {
            "ready": ready,
            "next_action": "answer" if ready else "searchSkillDocs",
            "reason": stop_reason,
            "stop": ready,
        }
        result: dict[str, Any] = {
            "selected_skills": selected,
            "retrieval_budget": {
                "max_docs": max_docs,
                "max_chars": max_chars,
                "used_docs": used_docs,
                "truncated": truncated,
            },
            "decision": decision,
        }
        if not ready:
            result["fallback_queries"] = self._fallback_queries(query, selected)
        if include_debug:
            result["debug"] = {
                "composition_plan": self._composition_plan(selected, allow_skill_chaining),
                "retrieval_budget": {
                    "max_skills": max_skills,
                    "effective_max_skills": effective_max_skills,
                    "max_docs": max_docs,
                    "max_chars": max_chars,
                    "used_docs": used_docs,
                    "used_chars": used_chars,
                    "truncated": truncated,
                },
                "fallback_queries": self._fallback_queries(query, selected),
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
        include_manifest: bool = True,
    ) -> dict[str, Any]:
        """Search a skill with SQLite FTS5 plus exact symbol boosting.

        Only ``keyword`` mode is currently implemented. ``semantic`` and ``hybrid``
        are intentionally not exposed until embeddings are added, because skill
        docs depend heavily on exact API, class, module, and constant names.
        """

        if mode != "keyword":
            raise SkillRuntimeError("Only keyword search mode is currently supported")

        skill = self._get_skill(skill_id)
        allowed_paths: set[str] | None = None
        if paths:
            allowed_paths = set()
            for rel_path in paths:
                self._resolve_path(skill, rel_path)  # validates path safety
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
        }

    def read(
        self,
        skill_id: str,
        path: str,
        start_line: int = 1,
        max_lines: int = 200,
        max_chars: int = 16_000,
    ) -> dict[str, Any]:
        """Read a skill file by safe relative path."""

        skill = self._get_skill(skill_id)
        file_path = self._resolve_path(skill, path)
        if not file_path.exists() or not file_path.is_file():
            raise SkillPathError(f"Skill file not found: {path}")

        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), start + max_lines - 1)
        selected = lines[start - 1 : end]
        content = "\n".join(selected)
        truncated = False
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = True

        return {
            "skill_id": skill.skill_id,
            "path": path,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "content": content,
            "content_hash": _content_hash(file_path),
            "truncated": truncated,
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

    def _read_skill_file(self, skill: Skill, path: str, max_chars: int | None = None) -> str:
        file_path = self._resolve_path(skill, path)
        if not file_path.exists() or not file_path.is_file():
            return ""
        return _read_text(file_path, max_chars=max_chars)

    def _candidate_paths(
        self,
        skill: Skill,
        paths: list[str] | None,
        include_manifest: bool,
    ) -> list[str]:
        if paths:
            return paths

        candidates: list[str] = []
        if include_manifest:
            candidates.append(skill.entrypoint)
        index_path = str(skill.metadata.get("index") or "INDEX.md")
        if (skill.root / index_path).exists():
            candidates.append(index_path)

        docs = skill.metadata.get("docs") or []
        for item in docs:
            if isinstance(item, dict) and item.get("path"):
                candidates.append(str(item["path"]))
            elif isinstance(item, str):
                candidates.append(item)

        docs_dir = skill.root / "docs"
        if docs_dir.exists():
            for file_path in sorted(docs_dir.rglob("*.md")):
                candidates.append(file_path.relative_to(skill.root).as_posix())
            for file_path in sorted(docs_dir.rglob("*.rst")):
                candidates.append(file_path.relative_to(skill.root).as_posix())

        return _unique_preserve_order(candidates)

    def _build_search_index(self) -> None:
        """Build an in-memory FTS5 index for all loaded skills."""

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
                        tags,
                        start_line UNINDEXED,
                        end_line UNINDEXED,
                        doc_kind UNINDEXED,
                        priority UNINDEXED,
                        content_hash UNINDEXED
                    )
                    """,
                )
            except sqlite3.OperationalError as exc:  # pragma: no cover - platform dependent
                raise SkillRuntimeError(
                    "SQLite FTS5 support is required for keyword search"
                ) from exc

            for skill in self._skills.values():
                for chunk in self._iter_search_chunks(skill):
                    self._search_db.execute(
                        """
                        INSERT INTO skill_docs_fts(
                            skill_id, path, title, heading_path, content, symbols, tags,
                            start_line, end_line, doc_kind, priority, content_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            skill.skill_id,
                            chunk["path"],
                            chunk["title"],
                            chunk["heading_path"],
                            chunk["content"],
                            " ".join(chunk["symbols"]),
                            " ".join(chunk["tags"]),
                            chunk["start_line"],
                            chunk["end_line"],
                            chunk["doc_kind"],
                            chunk["priority"],
                            chunk["content_hash"],
                        ),
                    )
            self._search_db.commit()

    def _iter_search_chunks(self, skill: Skill) -> list[dict[str, Any]]:
        doc_metadata = self._doc_metadata(skill)
        chunks: list[dict[str, Any]] = []
        for rel_path in self._candidate_paths(skill, paths=None, include_manifest=True):
            file_path = self._resolve_path(skill, rel_path)
            if not file_path.exists() or not file_path.is_file():
                continue
            metadata = doc_metadata.get(rel_path, {})
            text = file_path.read_text(encoding="utf-8", errors="replace")
            chunks.extend(self._chunk_file(skill, rel_path, text, metadata))
        return chunks

    def _doc_metadata(self, skill: Skill) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        docs = skill.metadata.get("docs") or []
        for item in docs:
            if isinstance(item, dict) and item.get("path"):
                result[str(item["path"])] = item
            elif isinstance(item, str):
                result[item] = {"path": item}
        result.setdefault(skill.entrypoint, {"path": skill.entrypoint, "title": skill.name})
        index_path = str(skill.metadata.get("index") or "INDEX.md")
        result.setdefault(index_path, {"path": index_path, "title": "Index"})
        return result

    def _chunk_file(
        self,
        skill: Skill,
        rel_path: str,
        text: str,
        metadata: dict[str, Any],
    ) -> list[dict[str, Any]]:
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
            section_lines = lines[start_index:end_index]
            if not section_lines:
                continue
            content = "\n".join(section_lines).strip()
            if not content:
                continue
            title = self._chunk_title(section_lines, metadata, rel_path)
            tags = [str(tag) for tag in metadata.get("tags", [])]
            symbols = self._extract_symbols("\n".join([rel_path, title, " ".join(tags), content]))
            chunks.append(
                {
                    "path": rel_path,
                    "title": title,
                    "heading_path": title,
                    "content": content,
                    "symbols": symbols,
                    "tags": tags,
                    "start_line": start_index + 1,
                    "end_line": end_index,
                    "doc_kind": self._doc_kind(skill, rel_path),
                    "priority": self._doc_priority(skill, rel_path, metadata),
                    "content_hash": _content_hash(self._resolve_path(skill, rel_path)),
                }
            )
        return chunks

    def _chunk_title(self, lines: list[str], metadata: dict[str, Any], rel_path: str) -> str:
        for line in lines[:5]:
            match = _HEADING_RE.match(line)
            if match:
                return match.group(2).strip()
        return str(metadata.get("title") or Path(rel_path).stem)

    def _doc_kind(self, skill: Skill, rel_path: str) -> str:
        if rel_path == skill.entrypoint:
            return "manifest"
        if rel_path == str(skill.metadata.get("index") or "INDEX.md"):
            return "index"
        if rel_path.endswith(".rst"):
            return "full_reference"
        return "summary_doc"

    def _doc_priority(self, skill: Skill, rel_path: str, metadata: dict[str, Any]) -> float:
        if "priority" in metadata:
            return float(metadata["priority"])
        kind = self._doc_kind(skill, rel_path)
        if kind == "manifest":
            return 50.0
        if kind == "index":
            return 30.0
        if kind == "summary_doc":
            return 20.0
        return 5.0

    def _extract_symbols(self, text: str) -> list[str]:
        symbols: list[str] = []
        for match in _BACKTICK_RE.findall(text):
            symbols.extend(_API_SYMBOL_RE.findall(match))
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", match):
                symbols.append(match)
        symbols.extend(_API_SYMBOL_RE.findall(text))
        normalized = []
        for symbol in symbols:
            clean = symbol.strip().strip("`.,:;()")
            if len(clean) >= 3:
                normalized.append(clean)
                if "." in clean:
                    normalized.extend(part for part in clean.split(".") if len(part) >= 3)
        return _unique_preserve_order(normalized)

    def _fts_query(self, query: str) -> str:
        terms = []
        for term in _FTS_TOKEN_RE.findall(query):
            term = term.lower()
            if len(term) < 2:
                continue
            terms.append(term)
        terms = _unique_preserve_order(terms)[:16]
        return " OR ".join(f'"{term}"' for term in terms)

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
                SELECT rowid, skill_id, path, title, heading_path, content, symbols, tags,
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
        seen_keys: set[tuple[str, int, int]] = set()
        for rank_index, row in enumerate(rows):
            rel_path = str(row["path"])
            if allowed_paths is not None and rel_path not in allowed_paths:
                continue
            if not include_manifest and row["doc_kind"] == "manifest":
                continue

            key = (rel_path, int(row["start_line"]), int(row["end_line"]))
            if key in seen_keys:
                continue
            seen_keys.add(key)

            row_symbols = set(str(row["symbols"] or "").split())
            row_tags = set(str(row["tags"] or "").split())
            heading_tokens = set(_tokens(str(row["heading_path"] or "")))
            path_tokens = set(_tokens(rel_path))
            row_priority = float(row["priority"] or 0.0)
            bm25_rank = float(row["bm25_rank"] or 0.0)

            symbol_overlap = query_symbols & row_symbols
            heading_overlap = query_terms & heading_tokens
            path_overlap = query_terms & path_tokens
            tag_overlap = query_terms & row_tags

            # FTS bm25 values are smaller when better. Rank position is stable and
            # easier to combine with exact symbol/path/heading boosts.
            fts_rank_score = 50.0 / (rank_index + 1)
            symbol_score = 100.0 * len(symbol_overlap)
            path_score = 40.0 * len(path_overlap)
            heading_score = 30.0 * len(heading_overlap)
            tag_score = 15.0 * len(tag_overlap)
            score = fts_rank_score + symbol_score + path_score + heading_score
            score += tag_score + row_priority
            rank_features = {
                "symbol_matches": sorted(symbol_overlap),
                "document_symbols": sorted(row_symbols)[:25],
                "path_matches": sorted(path_overlap),
                "heading_matches": sorted(heading_overlap),
                "tag_matches": sorted(tag_overlap),
                "fts_rank": bm25_rank,
                "fts_rank_score": round(fts_rank_score, 4),
                "symbol_score": round(symbol_score, 4),
                "path_score": round(path_score, 4),
                "heading_score": round(heading_score, 4),
                "tag_score": round(tag_score, 4),
                "doc_priority": row_priority,
            }

            content = str(row["content"] or "")
            excerpt = content[:max_chars_per_match]
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
                    "excerpt": excerpt,
                    "symbols": sorted(symbol_overlap),
                    "document_symbols": sorted(row_symbols)[:25],
                    "rank_features": rank_features,
                    "why_relevant": self._why_relevant(rank_features),
                    "content_hash": str(row["content_hash"]),
                }
            )

        scored.sort(key=lambda item: item["score"], reverse=True)
        return scored[:limit]

    def _title_for_path(self, text: str, path: str) -> str:
        for line in text.splitlines()[:20]:
            match = _HEADING_RE.match(line)
            if match:
                return match.group(2).strip()
        return Path(path).stem

    def _public_skill_metadata(self, skill: Skill) -> dict[str, Any]:
        return {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "version": skill.version,
            "description": skill.description,
            "aliases": skill.aliases,
            "trigger_terms": skill.trigger_terms,
            "skill_type": skill.skill_type,
            "capability_tags": skill.capability_tags,
            "domains": skill.domains,
            "conflicts_with": skill.conflicts_with,
            "can_chain_with": skill.can_chain_with,
            "manifest_hash": self._hash_if_exists(skill, skill.entrypoint),
        }

    def _hash_if_exists(self, skill: Skill, path: str) -> str | None:
        file_path = self._resolve_path(skill, path)
        if file_path.exists() and file_path.is_file():
            return _content_hash(file_path)
        return None

    def _manifest_summary(self, manifest_text: str) -> dict[str, Any]:
        frontmatter, body = _parse_frontmatter(manifest_text)
        critical_rules = _extract_bullets(_section_lines(body, "Critical Rules"))
        module_router = _extract_markdown_table(_section_lines(body, "Module Router"))
        anti_patterns = _extract_markdown_table(_section_lines(body, "Anti-Patterns"))
        first_lines = [line for line in body.splitlines() if line.strip()][:12]
        return {
            "frontmatter": frontmatter,
            "overview": "\n".join(first_lines),
            "critical_rules": critical_rules,
            "module_router": module_router,
            "anti_patterns": anti_patterns,
        }

    def _recommended_tools(self, skill: Skill) -> list[str]:
        tools = (
            skill.metadata.get("required_actions")
            or skill.metadata.get("recommended_tools")
            or []
        )
        return [str(tool) for tool in tools]

    def _execution_guidance(
        self,
        skill: Skill,
        manifest_summary: dict[str, Any],
        docs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        preferred_modules = []
        for row in manifest_summary.get("module_router", []):
            module = row.get("Module") or row.get("module")
            if module:
                preferred_modules.append(module)
        return {
            "answer_strategy": "Use the retrieved manifest rules first, then relevant docs.",
            "preferred_modules_or_topics": preferred_modules[:10],
            "retrieved_doc_paths": [doc["path"] for doc in docs],
            "policy": skill.policy,
        }

    def _operating_rules(self, manifest_summary: dict[str, Any], skill: Skill) -> list[str]:
        rules = [str(rule) for rule in manifest_summary.get("critical_rules", [])]
        if not rules and skill.policy:
            rules.extend(str(item) for item in skill.policy.get("suggested_checks", []))
        return rules[:8]

    def _response_contract(
        self,
        skill: Skill,
        manifest_summary: dict[str, Any],
        docs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        preferred = []
        for row in manifest_summary.get("module_router", []):
            module = row.get("Module") or row.get("module")
            if module:
                preferred.append(str(module))
        contract = dict(skill.metadata.get("response_contract") or {})
        must_include = [str(item) for item in contract.get("must_include", [])]
        if not must_include:
            must_include = self._default_must_include(skill)
        return {
            "expected_output": contract.get("expected_output")
            or skill.metadata.get(
                "expected_output",
                "Answer the user task using the selected skill instructions and evidence.",
            ),
            "must_include": must_include,
            "preferred_modules_or_topics": preferred[:8],
            "must_avoid": [
                str(item)
                for item in contract.get("must_avoid", skill.policy.get("must_avoid", []))
            ],
        }

    def _default_must_include(self, skill: Skill) -> list[str]:
        return [
            "Directly satisfy the user's requested output format.",
            "Name the relevant APIs, files, or tools used from the evidence.",
            "Include validation or dry-run guidance when the task can change external state.",
        ]

    def _answer_readiness(
        self,
        skill: Skill,
        docs: list[dict[str, Any]],
        truncated: bool,
    ) -> dict[str, Any]:
        if truncated:
            return {
                "ready": False,
                "reason": "The retrieval budget was exhausted before all matches were included.",
                "recommended_next_action": "searchSkillDocs",
            }
        if docs:
            return {
                "ready": True,
                "reason": "Manifest rules and relevant documentation snippets are available.",
                "recommended_next_action": "answer",
            }
        return {
            "ready": False,
            "reason": f"No relevant documentation snippets were retrieved for {skill.skill_id}.",
            "recommended_next_action": "searchSkillDocs",
        }

    def _evidence(
        self,
        docs: list[dict[str, Any]],
        include_debug: bool = False,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for doc in docs:
            item: dict[str, Any] = {
                "path": doc["path"],
                "section": doc.get("heading_path") or doc.get("title"),
                "why_relevant": doc.get("why_relevant", "Matched by keyword search."),
            }
            if include_debug:
                item["score"] = doc.get("score")
                item["rank_features"] = doc.get("rank_features", {})
            evidence.append(item)
        return evidence

    def _stop_reason(
        self,
        selected: list[dict[str, Any]],
        truncated: bool,
        not_ready_skill_ids: list[str],
    ) -> str:
        if truncated:
            return "Context budget was exhausted."
        if not selected:
            return "No skill matched the task."
        if not_ready_skill_ids:
            return f"More context is needed for: {', '.join(not_ready_skill_ids)}."
        return "Selected skill context is sufficient to answer."

    def _fallback_queries(self, query: str, selected: list[dict[str, Any]]) -> list[str]:
        fallback = [query]
        for packet in selected:
            tags = packet.get("capability_tags", [])[:4]
            docs = packet.get("debug", {}).get("retrieved_docs", [])
            paths = [doc["path"] for doc in docs[:2]]
            if tags:
                fallback.append(" ".join([packet["skill_id"], *tags]))
            if paths:
                fallback.append(" ".join([packet["skill_id"], *paths]))
        return _unique_preserve_order(fallback)[:5]

    def _composition_plan(
        self,
        selected: list[dict[str, Any]],
        allow_skill_chaining: bool,
    ) -> dict[str, Any]:
        if not selected:
            return {
                "enabled": allow_skill_chaining,
                "strategy": "No skill selected.",
                "skills": [],
            }
        if len(selected) == 1:
            return {
                "enabled": allow_skill_chaining,
                "strategy": "Use the selected primary skill only.",
                "skills": [{"skill_id": selected[0]["skill_id"], "role": "primary"}],
            }
        return {
            "enabled": allow_skill_chaining,
            "strategy": "Use the primary skill first, then secondary skills as supporting context.",
            "skills": [
                {"skill_id": item["skill_id"], "role": item["role"]} for item in selected
            ],
        }

    def _why_relevant(self, rank_features: dict[str, Any]) -> str:
        if rank_features.get("symbol_matches"):
            return "Matched exact API or symbol names."
        if rank_features.get("path_matches"):
            return "Matched path or module terms."
        if rank_features.get("heading_matches"):
            return "Matched section heading terms."
        if rank_features.get("tag_matches"):
            return "Matched document tags."
        return "Matched full-text keyword search."

    def _validation_guidance(self, skill: Skill) -> dict[str, Any]:
        policy = skill.policy
        return {
            "can_validate": bool(policy.get("can_validate", True)),
            "suggested_checks": policy.get("suggested_checks", []),
            "failure_behavior": policy.get("failure_behavior", []),
        }
