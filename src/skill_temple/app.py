"""FastAPI gateway for compiled-catalog Skill loading and Workspace Actions."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .runtime import (
    SkillNotFoundError,
    SkillPathError,
    env_value_from_environment_or_dotenv,
    load_runtime,
)
from .workspace_actions import register_workspace_actions


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoadSkillsRequest(StrictRequest):
    skill_ids: list[str] = Field(
        min_length=1,
        description="Exact Skill ids selected from the catalog in GPT Instructions.",
    )


class ReadSkillContentRequest(StrictRequest):
    skill_id: str
    path: str = Field(description="Relative path inside the selected Skill.")
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=2000, ge=1, le=10000)


class ErrorDetail(BaseModel):
    code: str
    message: str
    suggested_next_action: str


class StructuredErrorResponse(BaseModel):
    error: ErrorDetail


class LoadedSkillPacket(BaseModel):
    skill_id: str
    name: str
    description: str
    source_path: str
    content: str
    content_hash: str
    referenced_paths: list[str] = Field(default_factory=list)


class LoadSkillsResponse(BaseModel):
    skills: list[LoadedSkillPacket]
    loaded_skill_ids: list[str]


class ReadSkillContentResponse(BaseModel):
    skill_id: str
    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    content_hash: str
    truncated: bool
    next_start_line: int | None = None


def _normalize_server_url(server_url: str | None) -> str | None:
    if server_url is None:
        return None
    normalized = server_url.strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("server_url must be an absolute http(s) URL")
    return normalized


def _first_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _request_server_url(request: Request) -> str:
    proto = _first_header_value(request.headers.get("x-forwarded-proto"))
    host = _first_header_value(request.headers.get("x-forwarded-host"))
    prefix = _first_header_value(request.headers.get("x-forwarded-prefix")) or ""
    if proto and host:
        return _normalize_server_url(f"{proto}://{host}{prefix}") or ""
    return _normalize_server_url(str(request.base_url)) or ""


def create_app(skills_dir: str | Path | None = None, server_url: str | None = None) -> FastAPI:
    runtime = load_runtime(skills_dir)
    configured_server_url = _normalize_server_url(
        server_url or env_value_from_environment_or_dotenv("SKILL_TEMPLE_SERVER_URL")
    )

    app = FastAPI(
        title="Skill Temple Gateway",
        version="0.3.0",
        description=(
            "The GPT selects Skill ids from a catalog already present in its Instructions. "
            "The gateway loads only those SKILL.md files and any referenced files."
        ),
        openapi_url=None,
        servers=([{"url": configured_server_url}] if configured_server_url else None),
    )

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json(request: Request) -> dict[str, Any]:
        schema = copy.deepcopy(app.openapi())
        if "servers" not in schema:
            schema["servers"] = [{"url": _request_server_url(request)}]
        return schema

    @app.get("/health", include_in_schema=False)
    def health_check() -> dict[str, object]:
        return {"status": "ok", "skills_dir": str(runtime.skills_dir)}

    @app.get("/v1/skills", include_in_schema=False)
    def list_skills() -> dict[str, object]:
        return runtime.list_skills()

    @app.post(
        "/v1/skills/load",
        operation_id="loadSkills",
        response_model=LoadSkillsResponse,
        responses={404: {"model": StructuredErrorResponse}},
        summary="Load selected Skills.",
        description="Load complete SKILL.md files for exact ids selected from GPT Instructions.",
        openapi_extra={"x-openai-isConsequential": False},
    )
    def load_skills(request: LoadSkillsRequest) -> LoadSkillsResponse:
        try:
            return LoadSkillsResponse.model_validate(runtime.load_skills(request.skill_ids))
        except SkillNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "skill_not_found",
                        "message": str(exc),
                        "suggested_next_action": "check_skill_id",
                    }
                },
            ) from exc

    @app.post(
        "/v1/skills/read",
        operation_id="readSkillContent",
        response_model=ReadSkillContentResponse,
        responses={404: {"model": StructuredErrorResponse}},
        summary="Read a file from a selected Skill.",
        description="Read an exact relative path from a selected Skill with line continuation.",
        openapi_extra={"x-openai-isConsequential": False},
    )
    def read_skill_content(request: ReadSkillContentRequest) -> ReadSkillContentResponse:
        try:
            return ReadSkillContentResponse.model_validate(
                runtime.read(
                    request.skill_id,
                    request.path,
                    start_line=request.start_line,
                    max_lines=request.max_lines,
                )
            )
        except SkillNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "skill_not_found",
                        "message": str(exc),
                        "suggested_next_action": "check_skill_id",
                    }
                },
            ) from exc
        except SkillPathError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "unsafe_or_missing_path",
                        "message": str(exc),
                        "suggested_next_action": "check_path",
                    }
                },
            ) from exc

    register_workspace_actions(app)
    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Skill Temple gateway.")
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--server-url", default=None)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        create_app(args.skills_dir, server_url=args.server_url),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
