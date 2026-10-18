import json
import logging
import secrets
import time
import uuid
from urllib.parse import urlsplit

from fastapi import Depends, Request

from app.core.config import settings
from app.core.errors import Problem
from app.db.session import get_db
from app.services.auth import principal_for


async def csrf_protection(request: Request):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        raise Problem("csrf", "Cross-origin form submissions are not allowed.", 403)
    cookie = request.cookies.get("ig_csrf", "")
    token = request.headers.get("x-csrf-token", "")
    if not token and not request.url.path.startswith("/api/"):
        token = str((await request.form()).get("csrf_token", ""))
    if not cookie or not token or not secrets.compare_digest(cookie, token):
        raise Problem("csrf", "Missing or invalid CSRF token. Reload the page and try again.", 403)


def current_principal(request: Request, db=Depends(get_db)):
    csrf = (
        request.cookies.get("ig_csrf", "")
        if request.method not in ("GET", "HEAD", "OPTIONS")
        else None
    )
    return principal_for(db, request.cookies.get("ig_session"), csrf)


class UploadTooLarge(BaseException):
    # Bypass FastAPI's generic multipart -> 400 wrapper. This ASGI boundary catches it.
    pass


class RequestMiddleware:
    """ASGI receive wrapper rejects oversized chunked requests before multipart parsing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        request = Request(scope)
        csrf = request.cookies.get("ig_csrf") or secrets.token_urlsafe(32)
        scope["state"]["csrf"] = csrf
        upload = scope["path"] in ("/api/v1/imports", "/imports/upload")
        limit = settings().max_upload_bytes + 65536 if upload else 262144
        count, started = 0, False
        status_code = 500
        start = time.monotonic()

        async def bounded_receive():
            nonlocal count
            message = await receive()
            if message["type"] == "http.request":
                count += len(message.get("body", b""))
                if count > limit:
                    raise UploadTooLarge()
            return message

        async def decorated_send(message):
            nonlocal started, status_code
            if message["type"] == "http.response.start":
                started, status_code = True, message["status"]
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"same-origin"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                        ),
                        (b"cache-control", b"no-store"),
                    ]
                )
                if "ig_csrf" not in request.cookies:
                    secure = "; Secure" if settings().cookie_secure else ""
                    headers.append(
                        (b"set-cookie", f"ig_csrf={csrf}; Path=/; SameSite=Lax{secure}".encode())
                    )
                message["headers"] = headers
            await send(message)

        try:
            length = request.headers.get("content-length")
            if length and int(length) > limit:
                raise UploadTooLarge()
            await self.app(scope, bounded_receive, decorated_send)
        except UploadTooLarge:
            if started:
                raise
            payload = json.dumps(
                {
                    "code": "upload_limit",
                    "message": "Request exceeds the configured byte limit.",
                    "request_id": request_id,
                }
            ).encode()
            await decorated_send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": payload})
        finally:
            logging.getLogger("importguard.http").info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status_code,
                    "duration_ms": round((time.monotonic() - start) * 1000),
                },
            )
