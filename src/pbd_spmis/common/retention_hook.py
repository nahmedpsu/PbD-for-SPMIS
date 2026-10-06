"""Registers records with the retention engine when they are created.

Services call :func:`schedule` after creating a record that carries personal data. The retention
engine computes the expiry from the catalogue and later calls the owning service's
``/v1/internal/retention`` endpoint to execute the end action. Registration is best-effort:
a missing retention service must not block registration, but the gap is visible in the
retention dashboard ("records without schedule").
"""

from __future__ import annotations

from .clients import client
from .context import RequestContext
from .errors import UpstreamUnavailable


def schedule(
    caller: str, ctx: RequestContext | None, record_type: str, record_ref: str, program: str
) -> None:
    body = {"service": caller, "record_type": record_type, "record_ref": record_ref, "program": program}
    try:
        client("retention", caller=caller).post(
            "/v1/schedules", json=body, headers={"X-Correlation-ID": ctx.correlation_id} if ctx else None
        )
    except UpstreamUnavailable:
        return
