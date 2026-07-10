"""FastAPI gateway for GPT Actions.

The public surface is intentionally small:

- retrieveSkillContext: default first call for skill-backed tasks.
- searchSkillDocs: targeted follow-up retrieval.
- readSkillContent: precise file reading by safe path.

``listSkills`` and ``resolveSkill`` stay available as debug endpoints, but they
are intentionally hidden from the default OpenAPI schema used by GPT Actions.
The web console is also hidden from OpenAPI and may request debug output.
"""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .runtime import (
    SkillNotFoundError,
    SkillPathError,
    env_value_from_environment_or_dotenv,
    load_runtime,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveSkillRequest(StrictRequest):
    query: str = Field(..., description="The user's task or request text.")
    hinted_skill_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional explicit skill hints, for example ['idapython'] "
            "when user writes @idapython."
        ),
    )
    max_results: int = Field(default=3, ge=1, le=10)


class RetrieveSkillContextRequest(StrictRequest):
    query: str = Field(..., description="The user's original task or request text.")
    hinted_skill_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional explicit skill hints, for example ['idapython'] "
            "when user writes @idapython."
        ),
    )
    max_docs: int = Field(default=6, ge=1, le=20)
    allow_skill_chaining: bool = Field(
        default=False,
        description="Allow multiple cooperating skills in one retrieval result.",
    )


class ConsoleRetrieveRequest(RetrieveSkillContextRequest):
    include_debug: bool = Field(
        default=False,
        description="Return diagnostic fields such as manifest summaries and raw retrieved docs.",
    )


class SearchSkillDocsRequest(StrictRequest):
    skill_id: str = Field(..., description="Skill id to search, such as 'idapython'.")
    query: str = Field(..., description="Search query for the skill documentation.")
    paths: list[str] | None = Field(
        default=None,
        description="Optional safe relative file paths to restrict the search.",
    )
    limit: int = Field(default=5, ge=1, le=30)


class ReadSkillContentRequest(StrictRequest):
    skill_id: str = Field(..., description="Skill id to read from, such as 'idapython'.")
    path: str = Field(
        ...,
        description="Safe relative path inside the skill, for example docs/ida_hexrays.md.",
    )
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=2000)


class ErrorDetail(BaseModel):
    code: str
    message: str
    suggested_next_action: str


class StructuredErrorResponse(BaseModel):
    error: ErrorDetail


class EvidenceItem(BaseModel):
    path: str
    section: str | None = None
    why_relevant: str


class ResponseContract(BaseModel):
    expected_output: str
    must_include: list[str]
    preferred_modules_or_topics: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)


class ValidationGuidance(BaseModel):
    can_validate: bool
    suggested_checks: list[str] = Field(default_factory=list)
    failure_behavior: list[str] = Field(default_factory=list)


class SelectedSkillPacket(BaseModel):
    skill_id: str
    role: Literal["primary", "secondary"]
    confidence: float
    capability_tags: list[str] = Field(default_factory=list)
    operating_rules: list[str] = Field(default_factory=list)
    response_contract: ResponseContract
    evidence: list[EvidenceItem] = Field(default_factory=list)
    validation_guidance: ValidationGuidance


class RetrievalBudget(BaseModel):
    max_docs: int
    max_chars: int
    used_docs: int
    truncated: bool


class Decision(BaseModel):
    ready: bool
    next_action: Literal["answer", "searchSkillDocs"]
    reason: str
    stop: bool


class RetrieveSkillContextResponse(BaseModel):
    selected_skills: list[SelectedSkillPacket] = Field(default_factory=list)
    retrieval_budget: RetrievalBudget
    decision: Decision
    fallback_queries: list[str] | None = None


class SearchMatch(BaseModel):
    skill_id: str
    path: str
    title: str
    heading_path: str
    score: float
    mode: str
    engine: str
    start_line: int
    end_line: int
    excerpt: str
    symbols: list[str] = Field(default_factory=list)
    document_symbols: list[str] = Field(default_factory=list)
    rank_features: dict[str, Any] = Field(default_factory=dict)
    why_relevant: str
    content_hash: str


class SearchSkillDocsResponse(BaseModel):
    skill_id: str
    query: str
    mode: str
    engine: str
    matches: list[SearchMatch] = Field(default_factory=list)


class ReadSkillContentResponse(BaseModel):
    skill_id: str
    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    content_hash: str
    truncated: bool


def _normalize_server_url(server_url: str | None) -> str | None:
    if server_url is None:
        return None

    normalized = server_url.strip().rstrip("/")
    if not normalized:
        return None

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("server_url must be an absolute http(s) URL, for example https://example.com")
    return normalized


def _first_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _request_server_url(request: Request) -> str:
    forwarded_proto = _first_header_value(request.headers.get("x-forwarded-proto"))
    forwarded_host = _first_header_value(request.headers.get("x-forwarded-host"))
    forwarded_prefix = _first_header_value(request.headers.get("x-forwarded-prefix")) or ""

    if forwarded_proto and forwarded_host:
        forwarded_url = f"{forwarded_proto}://{forwarded_host}{forwarded_prefix}"
        return _normalize_server_url(forwarded_url) or ""

    return _normalize_server_url(str(request.base_url)) or ""


def create_app(skills_dir: str | Path | None = None, server_url: str | None = None) -> FastAPI:
    runtime = load_runtime(skills_dir)
    configured_server_url = _normalize_server_url(
        server_url or env_value_from_environment_or_dotenv("SKILL_TEMPLE_SERVER_URL")
    )

    app = FastAPI(
        title="Skill Temple Gateway",
        version="0.1.0",
        description=(
            "A local Skill Runtime gateway for Custom GPT Actions. It retrieves compact "
            "skill manifest rules and relevant documentation snippets without requiring "
            "Custom GPT Knowledge to unpack or index skill archives."
        ),
        openapi_url=None,
        servers=([{"url": configured_server_url}] if configured_server_url else None),
    )

    def structured_error(
        code: str,
        message: str,
        suggested_next_action: str,
    ) -> dict[str, object]:
        return {
            "error": {
                "code": code,
                "message": message,
                "suggested_next_action": suggested_next_action,
            }
        }

    def retrieve_context(
        request: RetrieveSkillContextRequest,
        include_debug: bool = False,
    ) -> dict[str, object]:
        return runtime.retrieve(
            query=request.query,
            hinted_skill_ids=request.hinted_skill_ids,
            max_skills=3 if request.allow_skill_chaining else 1,
            max_docs=request.max_docs,
            allow_skill_chaining=request.allow_skill_chaining,
            include_debug=include_debug,
        )

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json(request: Request) -> dict[str, Any]:
        schema = copy.deepcopy(app.openapi())
        if "servers" not in schema:
            schema["servers"] = [{"url": _request_server_url(request)}]
        return schema

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

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def console() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML)

    @app.post("/console/retrieve", include_in_schema=False)
    def console_retrieve(request: ConsoleRetrieveRequest) -> dict[str, object]:
        try:
            return retrieve_context(request, include_debug=request.include_debug)
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post(
        "/v1/skills/retrieve",
        operation_id="retrieveSkillContext",
        response_model=RetrieveSkillContextResponse,
        response_model_exclude_none=True,
        responses={404: {"model": StructuredErrorResponse}},
        summary=(
            "Retrieve the best matching skill rules and relevant documentation "
            "for a user task."
        ),
        description=(
            "Default first Action call for skill-backed tasks, including hints such as "
            "@idapython. Selects relevant skills, returns compact rules and documentation "
            "snippets, and reports whether follow-up search or file reads are needed."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def retrieve_skill_context(
        request: RetrieveSkillContextRequest,
    ) -> RetrieveSkillContextResponse:
        try:
            return RetrieveSkillContextResponse.model_validate(retrieve_context(request))
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post(
        "/v1/skills/search",
        operation_id="searchSkillDocs",
        response_model=SearchSkillDocsResponse,
        responses={404: {"model": StructuredErrorResponse}},
        summary="Search documentation for a specific skill.",
        description=(
            "Use after retrieveSkillContext when more specific documentation is needed, "
            "or when the user asks about exact APIs, constants, classes, or edge behavior."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def search_skill_docs(request: SearchSkillDocsRequest) -> SearchSkillDocsResponse:
        try:
            return SearchSkillDocsResponse.model_validate(
                runtime.search(
                    skill_id=request.skill_id,
                    query=request.query,
                    paths=request.paths,
                    limit=request.limit,
                    mode="keyword",
                    max_chars_per_match=2000,
                    include_manifest=False,
                )
            )
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc
        except SkillPathError as exc:
            detail = structured_error("unsafe_or_missing_path", str(exc), "check_path")
            raise HTTPException(status_code=404, detail=detail) from exc

    @app.post(
        "/v1/skills/read",
        operation_id="readSkillContent",
        response_model=ReadSkillContentResponse,
        responses={404: {"model": StructuredErrorResponse}},
        summary="Read a skill file by safe relative path.",
        description=(
            "Use for precise follow-up reads when retrieveSkillContext or searchSkillDocs "
            "identifies a specific file path. Paths are constrained to the selected skill root."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def read_skill_content(request: ReadSkillContentRequest) -> ReadSkillContentResponse:
        try:
            return ReadSkillContentResponse.model_validate(
                runtime.read(
                    skill_id=request.skill_id,
                    path=request.path,
                    start_line=request.start_line,
                    max_lines=request.max_lines,
                )
            )
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            raise HTTPException(status_code=404, detail=detail) from exc
        except SkillPathError as exc:
            detail = structured_error("unsafe_or_missing_path", str(exc), "check_path")
            raise HTTPException(status_code=404, detail=detail) from exc

    return app


CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Skill Temple Console</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 980px; }
    label { display: block; font-weight: 600; margin-top: 1rem; }
    input[type="text"], input[type="number"], textarea {
      width: 100%; box-sizing: border-box; padding: .55rem; font: inherit;
    }
    textarea { min-height: 7rem; }
    button { margin-top: 1rem; padding: .65rem 1rem; font: inherit; }
    pre { background: #111827; color: #e5e7eb; padding: 1rem; overflow: auto; }
    .row { display: flex; gap: 1rem; align-items: center; }
    .row label { font-weight: 400; margin-top: .75rem; }
  </style>
</head>
<body>
  <h1>Skill Temple Console</h1>
  <p>
    This development console is hidden from the GPT Action OpenAPI schema
    and may request debug output.
  </p>
  <label for="query">Query</label>
  <textarea id="query">@idapython write a script to find xrefs to strcpy</textarea>
  <label for="hints">Hinted skill ids, comma-separated</label>
  <input id="hints" type="text" value="idapython" />
  <label for="max_docs">Max docs</label>
  <input id="max_docs" type="number" min="1" max="20" value="6" />
  <div class="row">
    <label><input id="allow_chain" type="checkbox" /> Allow skill chaining</label>
    <label><input id="include_debug" type="checkbox" checked /> Include debug</label>
  </div>
  <button id="run">Retrieve</button>
  <h2>Result</h2>
  <pre id="result">Ready.</pre>
  <script>
    const result = document.getElementById('result');
    document.getElementById('run').addEventListener('click', async () => {
      const hinted = document.getElementById('hints').value
        .split(',').map(v => v.trim()).filter(Boolean);
      const body = {
        query: document.getElementById('query').value,
        hinted_skill_ids: hinted,
        max_docs: Number(document.getElementById('max_docs').value || 6),
        allow_skill_chaining: document.getElementById('allow_chain').checked,
        include_debug: document.getElementById('include_debug').checked
      };
      result.textContent = 'Loading...';
      try {
        const response = await fetch('/console/retrieve', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)
        });
        const data = await response.json();
        result.textContent = JSON.stringify(data, null, 2);
      } catch (error) {
        result.textContent = String(error);
      }
    });
  </script>
</body>
</html>
"""


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
    parser.add_argument(
        "--server-url",
        default=None,
        help=(
            "Public absolute http(s) URL to publish in OpenAPI servers. "
            "Can also be set with SKILL_TEMPLE_SERVER_URL."
        ),
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        create_app(args.skills_dir, server_url=args.server_url),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
