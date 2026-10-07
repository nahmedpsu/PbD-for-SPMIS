"""Break-glass manager.

"Exceptional access is a workflow, not an administrator back door." A grant names the subject,
the attributes, the reason and a duration. It must be requested with MFA, approved by a different
person with MFA, expires on its own, raises an alert when activated, and must be reviewed
afterwards. The PDP only honours a grant that is active, held by the requesting actor and scoped
to the subject being accessed.

POST /v1/grants                 request
POST /v1/grants/{id}/approve    approve (SUPERVISOR, not the requester)
POST /v1/grants/{id}/revoke
POST /v1/grants/{id}/review     post-event review (AUDITOR / SUPERVISOR / PRIVACY_ADMIN)
GET  /v1/grants/{id}
GET  /v1/grants?status=...      dashboard: active grants, overdue reviews
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..catalog.loader import get_catalog
from ..common.audit_client import emit
from ..common.context import RequestContext, service_context
from ..common.db import session_for
from ..common.errors import NotFound, PolicyDenied, ValidationFailed, install_error_handlers
from ..common.ids import opaque

SERVICE = "breakglass"
REQUEST_ROLES = {"CASE_WORKER", "SUPERVISOR"}
APPROVE_ROLES = {"SUPERVISOR"}
REVIEW_ROLES = {"AUDITOR", "SUPERVISOR", "PRIVACY_ADMIN"}
MAX_DURATION_MINUTES = 240
REVIEW_DUE_HOURS = 72


class Base(DeclarativeBase):
    pass


class Grant(Base):
    __tablename__ = "grants"
    grant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    requester_id: Mapped[str] = mapped_column(String(128), index=True)
    requester_role: Mapped[str] = mapped_column(String(64))
    approver_id: Mapped[str | None] = mapped_column(String(128), default=None)
    subject_token: Mapped[str] = mapped_column(String(16), index=True)
    program: Mapped[str] = mapped_column(String(64))
    attributes: Mapped[list[str]] = mapped_column(JSON, default=list)
    reason_code: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(500))
    case_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)
    requested_at: Mapped[str] = mapped_column(String(40))
    approved_at: Mapped[str | None] = mapped_column(String(40), default=None)
    expires_at: Mapped[str | None] = mapped_column(String(40), default=None)
    revoked_at: Mapped[str | None] = mapped_column(String(40), default=None)
    reviewed_at: Mapped[str | None] = mapped_column(String(40), default=None)
    reviewer_id: Mapped[str | None] = mapped_column(String(128), default=None)
    review_outcome: Mapped[str | None] = mapped_column(String(32), default=None)


class GrantIn(BaseModel):
    subject_token: str
    program: str
    attributes: list[str] = Field(min_length=1)
    reason_code: str = Field(pattern=r"^[a-z_]+$", description="e.g. immediate_risk, court_order")
    reason: str = Field(min_length=10, max_length=500)
    case_ref: str | None = None
    duration_minutes: int = Field(default=60, ge=5, le=MAX_DURATION_MINUTES)


class ReviewIn(BaseModel):
    outcome: str = Field(pattern=r"^(justified|unjustified|needs_follow_up)$")
    notes: str = Field(default="", max_length=500)


def _now() -> datetime:
    return datetime.now(UTC)


def _to_dict(g: Grant) -> dict[str, Any]:
    return {
        "grant_id": g.grant_id,
        "requester_id": g.requester_id,
        "requester_role": g.requester_role,
        "approver_id": g.approver_id,
        "subject_token": g.subject_token,
        "program": g.program,
        "attributes": g.attributes,
        "reason_code": g.reason_code,
        "case_ref": g.case_ref,
        "duration_minutes": g.duration_minutes,
        "status": g.status,
        "requested_at": g.requested_at,
        "approved_at": g.approved_at,
        "expires_at": g.expires_at,
        "revoked_at": g.revoked_at,
        "reviewed_at": g.reviewed_at,
        "review_outcome": g.review_outcome,
        "review_due_at": (
            datetime.fromisoformat(g.approved_at) + timedelta(hours=REVIEW_DUE_HOURS)
        ).isoformat()
        if g.approved_at
        else None,
    }


def _refresh_status(g: Grant) -> None:
    if g.status == "active" and g.expires_at and datetime.fromisoformat(g.expires_at) <= _now():
        g.status = "expired"


def _require_mfa(ctx: RequestContext) -> None:
    if not ctx.actor.has_mfa:
        raise PolicyDenied(
            "multi-factor authentication is required for exceptional access", code="MFA_REQUIRED"
        )


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Break-Glass Manager", version="0.2.0")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/grants", status_code=201)
    def request_grant(body: GrantIn, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        if ctx.actor.role not in REQUEST_ROLES:
            raise PolicyDenied("role may not request exceptional access", code="ROLE_NOT_PERMITTED")
        _require_mfa(ctx)
        cat = get_catalog()
        if body.program not in cat.programs:
            raise ValidationFailed("unknown program", code="unknown_program")
        unknown = [a for a in body.attributes if a not in cat.attributes]
        if unknown:
            raise ValidationFailed(f"unknown attributes {unknown}", code="unknown_attribute")
        g = Grant(
            grant_id=opaque("BG", 8),
            requester_id=ctx.actor.id,
            requester_role=ctx.actor.role,
            subject_token=body.subject_token,
            program=body.program,
            attributes=sorted(set(body.attributes)),
            reason_code=body.reason_code,
            reason=body.reason,
            case_ref=body.case_ref,
            duration_minutes=body.duration_minutes,
            status="pending",
            requested_at=_now().isoformat(),
        )
        with session_for(SERVICE, Base) as s:
            s.add(g)
            s.flush()
            out = _to_dict(g)
        emit(
            SERVICE,
            ctx,
            "break_glass_requested",
            outcome="pending",
            subject_token=body.subject_token,
            attributes=out["attributes"],
            program=body.program,
            purpose="emergency_protection",
            details={"grant_id": out["grant_id"], "reason_code": body.reason_code, "case_ref": body.case_ref},
        )
        return out

    @app.post("/v1/grants/{grant_id}/approve")
    def approve(grant_id: str, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        if ctx.actor.role not in APPROVE_ROLES:
            raise PolicyDenied("only supervisors approve exceptional access", code="ROLE_NOT_PERMITTED")
        _require_mfa(ctx)
        with session_for(SERVICE, Base) as s:
            g = s.get(Grant, grant_id)
            if g is None:
                raise NotFound("unknown grant")
            if g.requester_id == ctx.actor.id:
                raise PolicyDenied("requester cannot approve their own grant", code="SEPARATION_OF_DUTIES")
            if g.status != "pending":
                raise ValidationFailed(f"grant is {g.status}, not pending", code="invalid_state")
            now = _now()
            g.status, g.approver_id = "active", ctx.actor.id
            g.approved_at = now.isoformat()
            g.expires_at = (now + timedelta(minutes=g.duration_minutes)).isoformat()
            out = _to_dict(g)
        emit(
            SERVICE,
            ctx,
            "break_glass_activated",
            outcome="active",
            subject_token=out["subject_token"],
            attributes=out["attributes"],
            program=out["program"],
            purpose="emergency_protection",
            obligations=["alert", "review_required"],
            details={
                "grant_id": grant_id,
                "requester_id": out["requester_id"],
                "expires_at": out["expires_at"],
                "alert": "SECURITY_OPERATIONS_NOTIFIED",
            },
        )
        return out

    @app.post("/v1/grants/{grant_id}/revoke")
    def revoke(grant_id: str, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            g = s.get(Grant, grant_id)
            if g is None:
                raise NotFound("unknown grant")
            if ctx.actor.role not in APPROVE_ROLES | REVIEW_ROLES and ctx.actor.id != g.requester_id:
                raise PolicyDenied("not permitted to revoke this grant", code="ROLE_NOT_PERMITTED")
            if g.status in ("pending", "active"):
                g.status, g.revoked_at = "revoked", _now().isoformat()
            out = _to_dict(g)
        emit(
            SERVICE,
            ctx,
            "break_glass_revoked",
            outcome="revoked",
            subject_token=out["subject_token"],
            program=out["program"],
            purpose="emergency_protection",
            details={"grant_id": grant_id},
        )
        return out

    @app.post("/v1/grants/{grant_id}/review")
    def review(
        grant_id: str, body: ReviewIn, ctx: RequestContext = Depends(service_context)
    ) -> dict[str, Any]:
        if ctx.actor.role not in REVIEW_ROLES:
            raise PolicyDenied(
                "only auditors/supervisors review exceptional access", code="ROLE_NOT_PERMITTED"
            )
        with session_for(SERVICE, Base) as s:
            g = s.get(Grant, grant_id)
            if g is None:
                raise NotFound("unknown grant")
            if g.approved_at is None:
                raise ValidationFailed("grant was never activated", code="invalid_state")
            if g.requester_id == ctx.actor.id or g.approver_id == ctx.actor.id:
                raise PolicyDenied(
                    "reviewer must be independent of requester and approver", code="SEPARATION_OF_DUTIES"
                )
            _refresh_status(g)
            g.reviewed_at, g.reviewer_id, g.review_outcome = _now().isoformat(), ctx.actor.id, body.outcome
            if g.status in ("expired", "revoked"):
                g.status = "reviewed"
            out = _to_dict(g)
        emit(
            SERVICE,
            ctx,
            "break_glass_reviewed",
            outcome=body.outcome,
            subject_token=out["subject_token"],
            program=out["program"],
            purpose="emergency_protection",
            details={"grant_id": grant_id},
        )
        return out

    @app.get("/v1/grants/{grant_id}")
    def get_grant(grant_id: str, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            g = s.get(Grant, grant_id)
            if g is None:
                raise NotFound("unknown grant")
            allowed = (
                ctx.actor.is_service
                or ctx.actor.role in APPROVE_ROLES | REVIEW_ROLES
                or ctx.actor.id in (g.requester_id, g.approver_id)
            )
            if not allowed:
                raise PolicyDenied("not permitted to view this grant", code="ROLE_NOT_PERMITTED")
            _refresh_status(g)
            return _to_dict(g)

    @app.get("/v1/grants")
    def list_grants(
        ctx: RequestContext = Depends(service_context),
        status: str | None = Query(
            default=None, description="pending|active|expired|revoked|reviewed|overdue_review"
        ),
    ) -> dict[str, Any]:
        if not (ctx.actor.is_service or ctx.actor.role in APPROVE_ROLES | REVIEW_ROLES):
            raise PolicyDenied("not permitted to list grants", code="ROLE_NOT_PERMITTED")
        with session_for(SERVICE, Base) as s:
            rows = []
            for g in s.execute(select(Grant).order_by(Grant.requested_at.desc())).scalars():
                _refresh_status(g)
                d = _to_dict(g)
                if status == "overdue_review":
                    due = d["review_due_at"]
                    if g.reviewed_at is None and due and datetime.fromisoformat(due) < _now():
                        rows.append(d)
                elif status is None or d["status"] == status:
                    rows.append(d)
        return {"grants": rows, "count": len(rows)}

    return app
