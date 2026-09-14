from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.exceptions.parser_errors import ParserError
from backend.exceptions.workflow_errors import WorkflowError
from backend.exceptions.llm_errors import LLMError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(KeyError)
    async def key_error_handler(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": "bad_request", "detail": str(exc)})

    @app.exception_handler(ParserError)
    async def parser_error_handler(_: Request, exc: ParserError):
        return JSONResponse(status_code=422, content={"error": "parser_error", "detail": str(exc)})

    @app.exception_handler(WorkflowError)
    async def workflow_error_handler(_: Request, exc: WorkflowError):
        return JSONResponse(status_code=409, content={"error": "workflow_error", "detail": str(exc)})

    @app.exception_handler(LLMError)
    async def llm_error_handler(_: Request, exc: LLMError):
        return JSONResponse(status_code=502, content={"error": "llm_error", "detail": str(exc)})
