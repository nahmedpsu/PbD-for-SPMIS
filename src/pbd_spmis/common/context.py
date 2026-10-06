"""Request context: who is asking, for which program and purpose, in what situation.

Every sensitive endpoint derives a :class:`RequestContext` from the bearer token and the
``X-Purpose``, ``X-Program``, ``X-Correlation-ID``, ``X-Channel``, ``X-Device-Trust`` and
``X-Case-ID`` headers. The context is what the Policy Decision Point evaluates and what the
audit log records. It intentionally contains no personal data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, Request

from ..config import get_settings
from .auth import Actor, verify_token
from .errors import AuthenticationError, ValidationFailed
from .ids import correlation_id as new_correlation_id


@dataclass
class RequestContext:
    actor: Actor
    purpose: str
    program: str
    correlation_id: str
    channel: str = "api"
    device_trust: str = "managed"
    case_id: str | None = None
    break_glass_grant: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def policy_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "channel": self.channel,
            "device_trust": self.device_trust,
            "case_id": self.case_id,
            "correlation_id": self.correlation_id,
        }
        ctx.update(self.extras)
        return ctx

    def forward_headers(self, purpose: str | None = None, program: str | None = None) -> dict[str, str]:
        """Headers to propagate on downstream calls made on behalf of this request."""
        headers = {
            "X-Correlation-ID": self.correlation_id,
            "X-Purpose": purpose or self.purpose,
            "X-Program": program or self.program,
            "X-Channel": self.channel,
            "X-Device-Trust": self.device_trust,
        }
        if self.case_id:
            headers["X-Case-ID"] = self.case_id
        if self.break_glass_grant:
            headers["X-Break-Glass-Grant"] = self.break_glass_grant
        return headers


def actor_from_request(request: Request) -> Actor:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise AuthenticationError("bearer token required")
    settings = get_settings()
    return verify_token(settings.auth_signing_key, auth[7:].strip(), issuer=settings.auth_issuer)


def request_context(
    request: Request,
    x_purpose: str | None = Header(default=None, alias="X-Purpose"),
    x_program: str | None = Header(default=None, alias="X-Program"),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
    x_channel: str = Header(default="api", alias="X-Channel"),
    x_device_trust: str = Header(default="managed", alias="X-Device-Trust"),
    x_case_id: str | None = Header(default=None, alias="X-Case-ID"),
    x_break_glass_grant: str | None = Header(default=None, alias="X-Break-Glass-Grant"),
) -> RequestContext:
    """FastAPI dependency that builds the request context and authenticates the caller."""
    actor = actor_from_request(request)
    if not x_purpose:
        raise ValidationFailed("X-Purpose header is required", code="purpose_required")
    if not x_program:
        raise ValidationFailed("X-Program header is required", code="program_required")
    return RequestContext(
        actor=actor,
        purpose=x_purpose,
        program=x_program,
        correlation_id=x_correlation_id or new_correlation_id(),
        channel=x_channel,
        device_trust=x_device_trust,
        case_id=x_case_id,
        break_glass_grant=x_break_glass_grant,
    )


def service_context(
    request: Request,
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
) -> RequestContext:
    """Context for endpoints that need authentication but no purpose/program (e.g. audit writes)."""
    actor = actor_from_request(request)
    return RequestContext(
        actor=actor,
        purpose="",
        program="",
        correlation_id=x_correlation_id or new_correlation_id(),
    )
