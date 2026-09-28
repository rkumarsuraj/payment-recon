"""Uniform error envelope for every non-2xx response.

Every error response has the shape:

    {"error": {"code": ..., "message": ..., "details": [...], "request_id": ...}}

Nothing internal (SQL text, driver errors, stack traces, connection strings)
is ever put into a client-facing response body; those go to logs only,
keyed by request_id.
"""
import logging
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("setu.errors")


class AppError(Exception):
    """Raise this from application code for a specific, documented error."""

    def __init__(self, code: str, message: str, http_status: int, details: list | None = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or []


def _envelope(code: str, message: str, request_id: str, details: list | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or [],
            "request_id": request_id,
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, request_id, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        details = [
            {"field": ".".join(str(p) for p in err["loc"]), "issue": err["msg"]}
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_envelope("VALIDATION_ERROR", "Request validation failed", request_id, details),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else "HTTP_ERROR"
        message = exc.detail.get("message") if isinstance(exc.detail, dict) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code or "HTTP_ERROR", message or "Request failed", request_id),
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_db_error(request: Request, exc: SQLAlchemyError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        logger.error("db_error request_id=%s error=%s", request_id, exc, exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_envelope(
                "DATABASE_UNAVAILABLE", "The database is temporarily unavailable", request_id
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        logger.error("unhandled_error request_id=%s error=%s", request_id, exc, exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL_ERROR", "An unexpected error occurred", request_id),
        )
