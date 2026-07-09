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
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
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


def load_runtime(skills_dir: str | Path | None = None) -> SkillRuntime:
    """Create a runtime from an explicit path, environment, cwd, or packaged examples."""

    selected = _resolve_skills_dir(skills_dir)
    return SkillRuntime(selected)


def _resolve_skills_dir(skills_dir: str | Path | None) -> Path:
    if skills_dir:
        return Path(skills_dir).expanduser().resolve()

    env_value = os.environ.get("SKILL_TEMPLE_SKILLS_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()

    cwd_skills = Path.cwd() / "skills"
    if cwd_skills.exists():
        return cwd_skills.resolve()

    with resources.as_file(resources.files("skill_temple") / "example_skills") as path:
        return path.resolve()


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
        self._search_db = sqlite3.connect(":memory:")
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

            metadata_tokens = set(_tokens(" ".join([skill.name, skill.description, *skill.aliases])))
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

    def retrieve(
        self,
        query: str,
        hinted_skill_ids: list[str] | None = None,
        max_skills: int = DEFAULT_MAX_SKILLS,
        max_docs: int = DEFAULT_MAX_DOCS,
        max_chars: int = DEFAULT_MAX_CHARS,
        detail_level: str = "balanced",
        include_manifest: bool = True,
        include_policy: bool = True,
        include_recommended_tools: bool = True,
    ) -> dict[str, Any]:
        """Retrieve sufficient skill context for a user task in one call."""

        resolved = self.resolve(query, hinted_skill_ids=hinted_skill_ids, max_results=max_skills)
        selected: list[dict[str, Any]] = []
        budget_remaining = max_chars
        truncated = False

        for match in resolved["matches"][:max_skills]:
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
                docs.append(doc)

            selected.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "version": skill.version,
                    "confidence": match["confidence"],
                    "why_selected": match["reason"],
                    "manifest_hash": self._hash_if_exists(skill, skill.entrypoint),
                    "manifest_summary": manifest_summary,
                    "retrieved_docs": docs,
                    "tool_policy": skill.policy if include_policy else {},
                    "recommended_tools": self._recommended_tools(skill) if include_recommended_tools else [],
                    "execution_guidance": self._execution_guidance(skill, manifest_summary, docs),
                    "validation_guidance": self._validation_guidance(skill),
                }
            )

        need_more_context = truncated or not selected
        return {
            "selected_skills": selected,
            "retrieval_budget": {
                "max_skills": max_skills,
                "max_docs": max_docs,
                "max_chars": max_chars,
                "truncated": truncated,
            },
            "need_more_context": need_more_context,
            "recommended_next_action": "searchSkillDocs" if need_more_context else "answer",
            "reason": "Context budget was exhausted." if truncated else "Retrieved sufficient context.",
        }

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
                """
            )
        except sqlite3.OperationalError as exc:  # pragma: no cover - platform dependent
            raise SkillRuntimeError("SQLite FTS5 support is required for keyword search") from exc

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
            end_index = heading_indices[position + 1] if position + 1 < len(heading_indices) else len(lines)
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

            symbol_overlap = query_symbols & row_symbols
            heading_overlap = query_terms & heading_tokens
            path_overlap = query_terms & path_tokens
            tag_overlap = query_terms & row_tags

            # FTS bm25 values are smaller when better. Rank position is stable and
            # easier to combine with exact symbol/path/heading boosts.
            score = 50.0 / (rank_index + 1)
            score += 100.0 * len(symbol_overlap)
            score += 40.0 * len(path_overlap)
            score += 30.0 * len(heading_overlap)
            score += 15.0 * len(tag_overlap)
            score += float(row["priority"] or 0.0)

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
        tools = skill.metadata.get("required_actions") or skill.metadata.get("recommended_tools") or []
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

    def _validation_guidance(self, skill: Skill) -> dict[str, Any]:
        policy = skill.policy
        return {
            "can_validate": bool(policy.get("can_validate", True)),
            "suggested_checks": policy.get("suggested_checks", []),
            "failure_behavior": policy.get("failure_behavior", []),
        }
