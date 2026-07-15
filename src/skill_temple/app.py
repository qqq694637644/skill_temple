"""FastAPI gateway for compiled-catalog Skill loading and Workspace Actions."""

from __future__ import annotations

import argparse
import copy
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .runtime import (
    SkillNotFoundError,
    SkillPathError,
    env_value_from_environment_or_dotenv,
    load_runtime,
)
from .workspace_actions import register_workspace_actions

BEARER_TOKEN_ENV_VAR = "SKILL_TEMPLE_BEARER_TOKEN"


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


def _normalize_bearer_token(token: str | None) -> str | None:
    if token is None:
        return None
    normalized = token.strip()
    return normalized or None


def _requires_bearer_auth(path: str) -> bool:
    return path.startswith("/v1/") or path in {"/console/load", "/console/read"}


def _valid_bearer_authorization(authorization: str | None, expected_token: str) -> bool:
    if not authorization:
        return False
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(value.strip(), expected_token)


def _add_bearer_auth_security(schema: dict[str, Any]) -> dict[str, Any]:
    components = schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes["BearerAuth"] = {"type": "http", "scheme": "bearer"}
    for path, path_item in schema.get("paths", {}).items():
        if not path.startswith("/v1/"):
            continue
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.setdefault("security", [{"BearerAuth": []}])
    return schema


def _error(code: str, message: str, next_action: str) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "suggested_next_action": next_action,
        }
    }


def create_app(skills_dir: str | Path | None = None, server_url: str | None = None) -> FastAPI:
    runtime = load_runtime(skills_dir)
    configured_server_url = _normalize_server_url(
        server_url or env_value_from_environment_or_dotenv("SKILL_TEMPLE_SERVER_URL")
    )
    bearer_token = _normalize_bearer_token(
        env_value_from_environment_or_dotenv(BEARER_TOKEN_ENV_VAR)
    )

    app = FastAPI(
        title="Skill Temple Gateway",
        version="0.3.1",
        description=(
            "The GPT selects Skill ids from a catalog already present in its Instructions. "
            "The gateway loads only those SKILL.md files and any referenced files."
        ),
        openapi_url=None,
        servers=([{"url": configured_server_url}] if configured_server_url else None),
    )

    original_openapi = app.openapi

    def openapi_with_optional_bearer_auth() -> dict[str, Any]:
        schema = original_openapi()
        if bearer_token:
            _add_bearer_auth_security(schema)
        return schema

    app.openapi = openapi_with_optional_bearer_auth  # type: ignore[method-assign]

    @app.middleware("http")
    async def bearer_auth_middleware(request: Request, call_next: Any) -> Any:
        if bearer_token and _requires_bearer_auth(request.url.path):
            if not _valid_bearer_authorization(
                request.headers.get("authorization"), bearer_token
            ):
                return JSONResponse(
                    status_code=401,
                    content=_error(
                        "unauthorized",
                        "Missing or invalid Bearer token.",
                        "configure_bearer_auth",
                    ),
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    def load_selected(request: LoadSkillsRequest) -> dict[str, Any]:
        return runtime.load_skills(request.skill_ids)

    def read_selected(request: ReadSkillContentRequest) -> dict[str, Any]:
        return runtime.read(
            request.skill_id,
            request.path,
            start_line=request.start_line,
            max_lines=request.max_lines,
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

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def console() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML)

    @app.post("/console/load", include_in_schema=False)
    def console_load(request: LoadSkillsRequest) -> dict[str, Any]:
        try:
            return load_selected(request)
        except SkillNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("skill_not_found", str(exc), "check_skill_id"),
            ) from exc

    @app.post("/console/read", include_in_schema=False)
    def console_read(request: ReadSkillContentRequest) -> dict[str, Any]:
        try:
            return read_selected(request)
        except SkillNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("skill_not_found", str(exc), "check_skill_id"),
            ) from exc
        except SkillPathError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("unsafe_or_missing_path", str(exc), "check_path"),
            ) from exc

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
            return LoadSkillsResponse.model_validate(load_selected(request))
        except SkillNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("skill_not_found", str(exc), "check_skill_id"),
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
            return ReadSkillContentResponse.model_validate(read_selected(request))
        except SkillNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("skill_not_found", str(exc), "check_skill_id"),
            ) from exc
        except SkillPathError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("unsafe_or_missing_path", str(exc), "check_path"),
            ) from exc

    register_workspace_actions(app)
    return app


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Skill Temple Retrieval Console</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 980px; }
    label { display: block; font-weight: 600; margin-top: 1rem; }
    input, textarea { width: 100%; box-sizing: border-box; padding: .55rem; }
    button { margin: 1rem .5rem 0 0; padding: .65rem 1rem; }
    pre { background: #111827; color: #e5e7eb; padding: 1rem; overflow: auto; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  </style>
</head>
<body>
  <h1>Skill Temple Retrieval Console</h1>
  <p>Debug the compiled catalog, exact Skill loading, and referenced-file reading.</p>
  <label for="token">Bearer token</label>
  <input id="token" type="password" placeholder="Optional token from .env" />
  <label for="skill_ids">Skill ids, comma-separated</label>
  <input id="skill_ids" value="idapython" />
  <div class="row">
    <div>
      <label for="read_skill_id">Read skill id</label>
      <input id="read_skill_id" value="idapython" />
    </div>
    <div>
      <label for="read_path">Relative path</label>
      <input id="read_path" value="SKILL.md" />
    </div>
  </div>
  <button id="catalog">Catalog</button>
  <button id="load">Load Skills</button>
  <button id="read">Read File</button>
  <h2>Result</h2>
  <pre id="result">Ready.</pre>
  <script>
    const tokenInput = document.getElementById('token');
    tokenInput.value = sessionStorage.getItem('skillTempleToken') || '';
    function headers() {
      const token = tokenInput.value.trim();
      sessionStorage.setItem('skillTempleToken', token);
      const value = {'Content-Type': 'application/json'};
      if (token) value.Authorization = `Bearer ${token}`;
      return value;
    }
    async function run(url, options = {}) {
      const result = document.getElementById('result');
      result.textContent = 'Loading...';
      try {
        const response = await fetch(url, {headers: headers(), ...options});
        const text = await response.text();
        let body;
        try { body = JSON.parse(text); } catch { body = text; }
        result.textContent = JSON.stringify({status: response.status, body}, null, 2);
      } catch (error) {
        result.textContent = String(error);
      }
    }
    document.getElementById('catalog').onclick = () => run('/v1/skills');
    document.getElementById('load').onclick = () => run('/console/load', {
      method: 'POST',
      body: JSON.stringify({
        skill_ids: document.getElementById('skill_ids').value
          .split(',').map(value => value.trim()).filter(Boolean)
      })
    });
    document.getElementById('read').onclick = () => run('/console/read', {
      method: 'POST',
      body: JSON.stringify({
        skill_id: document.getElementById('read_skill_id').value.trim(),
        path: document.getElementById('read_path').value.trim(),
        start_line: 1,
        max_lines: 2000
      })
    });
  </script>
</body>
</html>
"""


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
