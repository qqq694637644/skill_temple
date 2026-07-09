"""FastAPI gateway for GPT Actions.

The public surface is intentionally small:

- retrieveSkillContext: default first call for skill-backed tasks.
- searchSkillDocs: targeted follow-up retrieval.
- readSkillContent: precise file reading by safe path.

``listSkills`` and ``resolveSkill`` stay available as debug endpoints, but they
are intentionally hidden from the default OpenAPI schema used by GPT Actions.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .runtime import SkillNotFoundError, SkillPathError, load_runtime


class ResolveSkillRequest(BaseModel):
    query: str = Field(..., description="The user's task or request text.")
    hinted_skill_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional explicit skill hints, for example ['idapython'] "
            "when user writes @idapython."
        ),
    )
    max_results: int = Field(default=3, ge=1, le=10)


class RetrieveSkillContextRequest(BaseModel):
    query: str = Field(..., description="The user's original task or request text.")
    hinted_skill_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional explicit skill hints, for example ['idapython'] "
            "when user writes @idapython."
        ),
    )
    max_skills: int = Field(default=1, ge=1, le=5)
    max_docs: int = Field(default=6, ge=1, le=20)
    max_chars: int = Field(default=12_000, ge=1000, le=80_000)
    detail_level: Literal["brief", "balanced", "deep"] = Field(default="balanced")
    include_manifest: bool = True
    include_policy: bool = True
    include_recommended_tools: bool = True


class SearchSkillDocsRequest(BaseModel):
    skill_id: str = Field(..., description="Skill id to search, such as 'idapython'.")
    query: str = Field(..., description="Search query for the skill documentation.")
    paths: list[str] | None = Field(
        default=None,
        description="Optional safe relative file paths to restrict the search.",
    )
    limit: int = Field(default=5, ge=1, le=30)
    mode: Literal["keyword"] = Field(
        default="keyword",
        description="Only keyword mode is currently supported: SQLite FTS5 plus symbol boosting.",
    )
    max_chars_per_match: int = Field(default=2000, ge=200, le=20_000)
    include_manifest: bool = True


class ReadSkillContentRequest(BaseModel):
    skill_id: str = Field(..., description="Skill id to read from, such as 'idapython'.")
    path: str = Field(
        ...,
        description="Safe relative path inside the skill, for example docs/ida_hexrays.md.",
    )
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=2000)
    max_chars: int = Field(default=16_000, ge=100, le=100_000)


def create_app(skills_dir: str | Path | None = None) -> FastAPI:
    runtime = load_runtime(skills_dir)

    app = FastAPI(
        title="Skill Temple Gateway",
        version="0.1.0",
        description=(
            "A local Skill Runtime gateway for Custom GPT Actions. It retrieves compact "
            "skill manifest rules and relevant documentation snippets without requiring "
            "Custom GPT Knowledge to unpack or index skill archives."
        ),
    )

    @app.get(
        "/health",
        operation_id="healthCheck",
        summary="Check gateway health.",
        include_in_schema=False,
    )
    def health_check() -> dict[str, object]:
        return {"status": "ok", "skills_dir": str(runtime.skills_dir)}

    @app.get(
        "/v1/skills",
        operation_id="listSkills",
        summary="List available reusable skills.",
        description=(
            "Use for setup or debugging. Normal GPT workflows should usually call "
            "retrieveSkillContext directly with the user's task."
        ),
        include_in_schema=False,
    )
    def list_skills() -> dict[str, object]:
        return runtime.list_skills()

    @app.post(
        "/v1/skills/resolve",
        operation_id="resolveSkill",
        summary="Resolve which skill best matches a user task.",
        description=(
            "Ranks available skills for a task. This is useful for diagnostics; "
            "retrieveSkillContext already performs resolution internally."
        ),
        include_in_schema=False,
    )
    def resolve_skill(request: ResolveSkillRequest) -> dict[str, object]:
        return runtime.resolve(
            query=request.query,
            hinted_skill_ids=request.hinted_skill_ids,
            max_results=request.max_results,
        )

    @app.post(
        "/v1/skills/retrieve",
        operation_id="retrieveSkillContext",
        summary=(
            "Retrieve the best matching skill rules and relevant documentation "
            "for a user task."
        ),
        description=(
            "Use this as the default first Action call when a task may require a reusable skill, "
            "including explicit hints such as @idapython. The endpoint selects relevant skills, "
            "returns compact manifest rules, retrieves task-relevant documentation snippets, "
            "and reports whether more search or precise file reading is needed."
        ),
    )
    def retrieve_skill_context(request: RetrieveSkillContextRequest) -> dict[str, object]:
        return runtime.retrieve(
            query=request.query,
            hinted_skill_ids=request.hinted_skill_ids,
            max_skills=request.max_skills,
            max_docs=request.max_docs,
            max_chars=request.max_chars,
            detail_level=request.detail_level,
            include_manifest=request.include_manifest,
            include_policy=request.include_policy,
            include_recommended_tools=request.include_recommended_tools,
        )

    @app.post(
        "/v1/skills/search",
        operation_id="searchSkillDocs",
        summary="Search documentation for a specific skill.",
        description=(
            "Use after retrieveSkillContext when more specific documentation is needed, "
            "or when the user asks about exact APIs, constants, classes, or edge behavior."
        ),
    )
    def search_skill_docs(request: SearchSkillDocsRequest) -> dict[str, object]:
        try:
            return runtime.search(
                skill_id=request.skill_id,
                query=request.query,
                paths=request.paths,
                limit=request.limit,
                mode=request.mode,
                max_chars_per_match=request.max_chars_per_match,
                include_manifest=request.include_manifest,
            )
        except (SkillNotFoundError, SkillPathError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/v1/skills/read",
        operation_id="readSkillContent",
        summary="Read a skill file by safe relative path.",
        description=(
            "Use for precise follow-up reads when retrieveSkillContext or searchSkillDocs "
            "identifies a specific file path. Paths are constrained to the selected skill root."
        ),
    )
    def read_skill_content(request: ReadSkillContentRequest) -> dict[str, object]:
        try:
            return runtime.read(
                skill_id=request.skill_id,
                path=request.path,
                start_line=request.start_line,
                max_lines=request.max_lines,
                max_chars=request.max_chars,
            )
        except (SkillNotFoundError, SkillPathError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Skill Temple GPT Action gateway.")
    parser.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help="Directory containing skill folders.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(args.skills_dir), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
