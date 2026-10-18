import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from app.api.routes import router as api_router
from app.core.errors import Problem
from app.core.http import RequestMiddleware, csrf_protection
from app.core.logging import configure_logging

configure_logging()
app = FastAPI(
    title="ImportGuard",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    dependencies=[Depends(csrf_protection)],
)
app.add_middleware(RequestMiddleware)
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static", check_dir=False),
    name="static",
)


async def problem_response(request, code, message, status):
    if not request.url.path.startswith("/api/") and "text/html" in request.headers.get(
        "accept", ""
    ):
        if status == 401:
            return RedirectResponse("/login", status_code=303)
        from app.web.routes import render

        return render(request, "error.html", {"code": code, "message": message}, status=status)
    return JSONResponse(
        {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
        status_code=status,
    )


@app.exception_handler(Problem)
async def handle_problem(request: Request, error: Problem):
    return await problem_response(request, error.code, error.message, error.status)


@app.exception_handler(RequestValidationError)
async def handle_validation(request: Request, error: RequestValidationError):
    message = "; ".join(
        ".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in error.errors()
    )
    return await problem_response(request, "validation_error", message, 422)


@app.exception_handler(IntegrityError)
async def handle_integrity(request: Request, error: IntegrityError):
    return await problem_response(
        request,
        "constraint_conflict",
        "A conflicting record already exists or a related record changed.",
        409,
    )


@app.exception_handler(HTTPException)
async def handle_http(request: Request, error: HTTPException):
    return await problem_response(request, "http_error", str(error.detail), error.status_code)


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, error: Exception):
    logging.getLogger(__name__).exception(
        "request_failed", extra={"request_id": getattr(request.state, "request_id", "unknown")}
    )
    return await problem_response(
        request,
        "internal_error",
        "The request failed. Use the request ID to find its server log.",
        500,
    )


app.include_router(api_router)
from app.web.routes import router as web_router  # noqa: E402

app.include_router(web_router)
