"""Helper used by every service to emit audit events to the Audit Store.

Audit writes are best-effort from the caller's perspective *except* for disclosures and
exceptional access, which fail closed: if the audit store cannot record a sensitive release, the
release does not happen.
"""

from __future__ import annotations

from typing import Any

from .clients import client
from .context import RequestContext
from .errors import UpstreamUnavailable

MUST_RECORD = {"external_disclosure", "break_glass_activated", "token_resolved", "export"}


def emit(
    caller: str,
    ctx: RequestContext | None,
    event_type: str,
    *,
    outcome: str,
    subject_token: str | None = None,
    attributes: list[str] | None = None,
    decision_id: str | None = None,
    reason_codes: list[str] | None = None,
    obligations: list[str] | None = None,
    details: dict[str, Any] | None = None,
    purpose: str | None = None,
    program: str | None = None,
) -> str | None:
    body = {
        "event_type": event_type,
        "service": caller,
        "actor_id": ctx.actor.id if ctx else f"svc:{caller}",
        "actor_role": ctx.actor.role if ctx else f"{caller.upper()}_SERVICE",
        "actor_agency": ctx.actor.agency if ctx else "SOCIAL_PROTECTION_AGENCY",
        "purpose": purpose or (ctx.purpose if ctx else None),
        "program": program or (ctx.program if ctx else None),
        "subject_token": subject_token,
        "attributes": attributes or [],
        "outcome": outcome,
        "decision_id": decision_id,
        "reason_codes": reason_codes or [],
        "obligations": obligations or [],
        "correlation_id": ctx.correlation_id if ctx else None,
        "details": details or {},
    }
    try:
        resp = client("audit", caller=caller).post("/v1/events", json=body)
    except UpstreamUnavailable:
        if event_type in MUST_RECORD:
            raise
        return None
    if resp.status_code >= 300:
        if event_type in MUST_RECORD:
            raise UpstreamUnavailable(f"audit store rejected event: {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json().get("event_id")
