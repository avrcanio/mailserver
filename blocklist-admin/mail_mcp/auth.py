import secrets
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class BearerTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str, public_paths: set[str] | None = None):
        super().__init__(app)
        self.token = token
        self.public_paths = public_paths or {"/healthz"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self.public_paths:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        scheme, _, credential = auth.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not secrets.compare_digest(credential.strip(), self.token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
