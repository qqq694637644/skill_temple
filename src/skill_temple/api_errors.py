"""Shared structured API error responses."""

from __future__ import annotations

from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    suggested_next_action: str


class StructuredErrorResponse(BaseModel):
    error: ErrorDetail


def error_payload(code: str, message: str, next_action: str) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "suggested_next_action": next_action,
        }
    }


def error_response(
    status_code: int,
    code: str,
    message: str,
    next_action: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message, next_action),
        headers=headers,
    )
