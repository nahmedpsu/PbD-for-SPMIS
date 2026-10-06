"""Error model shared by every service.

Errors carry a stable machine-readable ``code`` so that clients, dashboards and the
conformance suite can rely on them, and they never include personal data.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ServiceError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, detail: str = "", *, code: str | None = None, extra: dict[str, Any] | None = None):
        super().__init__(detail or self.code)
        self.detail = detail or self.code
        if code:
            self.code = code
        self.extra = extra or {}


class AuthenticationError(ServiceError):
    status_code = 401
    code = "authentication_required"


class PolicyDenied(ServiceError):
    status_code = 403
    code = "policy_denied"


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class ValidationFailed(ServiceError):
    status_code = 422
    code = "validation_failed"


class UpstreamUnavailable(ServiceError):
    status_code = 503
    code = "upstream_unavailable"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.code, "detail": exc.detail}
        if exc.extra:
            body.update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content=body)
