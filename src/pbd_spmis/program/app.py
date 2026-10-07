"""Program Store service: enrollments, entitlements and cases.

Records are keyed by the program-specific identifier issued by the vault, so a program store
leaked on its own cannot be joined to another program's store or to the identity vault.

Endpoints
---------
POST /v1/enrollments                    enrol an eligible person (purpose: enrollment)
GET  /v1/enrollments/{program_person_id}
POST /v1/cases                          open a case (purpose: case_management)
GET  /v1/cases/{case_id}                read a case; requires X-Case-ID assignment
POST /v1/internal/retention
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..catalog.loader import get_catalog
from ..common import pep
from ..common.audit_client import emit
from ..common.clients import client
from ..common.context import RequestContext, request_context
from ..common.db import session_for
from ..common.errors import (
    NotFound,
    PolicyDenied,
    UpstreamUnavailable,
    ValidationFailed,
    install_error_handlers,
)
from ..common.ids import opaque
from ..common.retention_hook import schedule

SERVICE = "program"


class Base(DeclarativeBase):
    pass


class Enrollment(Base):
    __tablename__ = "enrollments"
    program_person_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    program: Mapped[str] = mapped_column(String(64), index=True)
    person_token: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(32))
    entitlement: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    eligibility_outcome: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))
    archived_at: Mapped[str | None] = mapped_column(String(40), default=None)


class Case(Base):
    __tablename__ = "cases"
    case_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    program_person_id: Mapped[str] = mapped_column(String(32), index=True)
    person_token: Mapped[str] = mapped_column(String(16), index=True)
    program: Mapped[str] = mapped_column(String(64))
    sensitivity: Mapped[str] = mapped_column(String(16), default="standard")
    status: Mapped[str] = mapped_column(String(32), default="open")
    narrative: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40))


class EnrollmentIn(BaseModel):
    person_token: str
    determination_id: str


class CaseIn(BaseModel):
    program_person_id: str
    case_id: str | None = None
    sensitivity: str = "standard"
    narrative: str = Field(default="", max_length=4000)


class RetentionIn(BaseModel):
    record_type: str
    record_ref: str
    action: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _entitlement(program: str, household_size: int | None) -> dict[str, Any]:
    spec = get_catalog().programs[program].get("entitlement", {})
    base = float(spec.get("base_amount", 0))
    per_member = float(spec.get("per_member", 0))
    extra_members = max((household_size or 1) - 1, 0)
    amount = min(base + per_member * extra_members, float(spec.get("max_amount", base)))
    return {
        "amount": amount,
        "currency": spec.get("currency", "XSP"),
        "period": spec.get("period", "monthly"),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Program Store", version="0.2.1")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/enrollments", status_code=201)
    def enrol(
        body: EnrollmentIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        if ctx.program not in get_catalog().programs:
            raise ValidationFailed("unknown program", code="unknown_program")
        fwd = ctx.forward_headers(purpose="enrollment")
        rel = client("registry", caller=SERVICE).get(
            f"/v1/persons/{body.person_token}/relationships", headers=fwd
        )
        if rel.status_code != 200:
            raise NotFound("person is not registered")
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=body.person_token,
            subject_programs=rel.json()["programs"],
            attributes=["household_size", "eligibility_outcome"],
            action="write",
            purpose="enrollment",
        )
        det = client("eligibility", caller=SERVICE).get(
            f"/v1/eligibility/determinations/{body.determination_id}", headers=fwd
        )
        if det.status_code != 200:
            raise ValidationFailed(
                "eligibility determination not found or not accessible", code="determination_unavailable"
            )
        outcome = det.json()["attributes"].get("eligibility_outcome", {})
        if outcome.get("program") != ctx.program or outcome.get("person_token") != body.person_token:
            raise ValidationFailed(
                "determination does not belong to this person/program", code="determination_mismatch"
            )
        if outcome.get("outcome") != "eligible":
            raise PolicyDenied(
                "person is not eligible", code="not_eligible", extra={"outcome": outcome.get("outcome")}
            )
        reg = client("registry", caller=SERVICE).get(
            f"/v1/persons/{body.person_token}", headers=fwd, params={"attributes": "household_size"}
        )
        household_size = reg.json()["attributes"].get("household_size") if reg.status_code == 200 else None
        ppid_resp = client("vault", caller=SERVICE).post(
            "/v1/program-identifiers",
            headers=fwd,
            json={"person_token": body.person_token, "program": ctx.program},
        )
        if ppid_resp.status_code not in (200, 201):
            raise UpstreamUnavailable(f"vault refused program identifier: {ppid_resp.status_code}")
        ppid = ppid_resp.json()["program_person_id"]
        entitlement = _entitlement(ctx.program, household_size)
        with session_for(SERVICE, Base) as s:
            existing = s.get(Enrollment, ppid)
            if existing:
                response.status_code = 200
                pep.attach_obligations(response, decision)
                return {
                    "program_person_id": ppid,
                    "status": existing.status,
                    "entitlement": existing.entitlement,
                    "created": False,
                }
            s.add(
                Enrollment(
                    program_person_id=ppid,
                    program=ctx.program,
                    person_token=body.person_token,
                    status="enrolled",
                    entitlement=entitlement,
                    eligibility_outcome={"determination_id": body.determination_id, "outcome": "eligible"},
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        client("registry", caller=SERVICE).post(
            f"/v1/persons/{body.person_token}/relationships", headers=fwd, json={"program": ctx.program}
        )
        schedule(SERVICE, ctx, "program_store.enrollment", ppid, ctx.program)
        emit(
            SERVICE,
            ctx,
            "enrollment_approved",
            outcome="enrolled",
            subject_token=body.person_token,
            decision_id=decision.decision_id,
            details={"program_person_id": ppid, "entitlement": entitlement},
        )
        pep.attach_obligations(response, decision)
        return {"program_person_id": ppid, "status": "enrolled", "entitlement": entitlement, "created": True}

    @app.get("/v1/enrollments/{program_person_id}")
    def read_enrollment(
        program_person_id: str, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            e = s.get(Enrollment, program_person_id)
            if e is None:
                raise NotFound("unknown program identifier")
            raw = {
                "enrollment_status": e.status,
                "entitlement": e.entitlement,
                "eligibility_outcome": e.eligibility_outcome,
            }
            token, program = e.person_token, e.program
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=token,
            subject_programs=[program],
            attributes=list(raw),
            program=program,
        )
        pep.attach_obligations(response, decision)
        return {
            "program_person_id": program_person_id,
            "attributes": pep.apply_release(raw, decision, program=program),
            "decision_id": decision.decision_id,
        }

    @app.post("/v1/cases", status_code=201)
    def open_case(
        body: CaseIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            e = s.get(Enrollment, body.program_person_id)
            if e is None:
                raise NotFound("unknown program identifier")
            token, program = e.person_token, e.program
        case_id = body.case_id or opaque("CASE", 8)
        ctx.case_id = ctx.case_id or case_id
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=token,
            subject_programs=[program],
            attributes=["case_narrative"],
            action="write",
            purpose="case_management",
            program=program,
        )
        with session_for(SERVICE, Base) as s:
            s.add(
                Case(
                    case_id=case_id,
                    program_person_id=body.program_person_id,
                    person_token=token,
                    program=program,
                    sensitivity=body.sensitivity,
                    narrative=body.narrative,
                    created_at=_now(),
                )
            )
        emit(
            SERVICE,
            ctx,
            "case_opened",
            outcome="created",
            subject_token=token,
            decision_id=decision.decision_id,
            details={"case_id": case_id, "sensitivity": body.sensitivity},
        )
        pep.attach_obligations(response, decision)
        return {"case_id": case_id, "program_person_id": body.program_person_id}

    @app.get("/v1/cases/{case_id}")
    def read_case(
        case_id: str, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            c = s.get(Case, case_id)
            if c is None:
                raise NotFound("unknown case")
            raw = {"case_narrative": c.narrative}
            token, program, meta = (
                c.person_token,
                c.program,
                {"status": c.status, "sensitivity": c.sensitivity},
            )
        ctx.case_id = ctx.case_id or case_id
        if ctx.case_id != case_id:
            raise PolicyDenied("X-Case-ID does not match the requested case", code="case_mismatch")
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=token,
            subject_programs=[program],
            attributes=["case_narrative"],
            purpose="case_management",
            program=program,
        )
        pep.attach_obligations(response, decision)
        return {
            "case_id": case_id,
            **meta,
            "attributes": pep.apply_release(raw, decision, program=program),
            "decision_id": decision.decision_id,
        }

    @app.post("/v1/internal/retention")
    def retention(body: RetentionIn, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
        if body.record_type != "program_store.enrollment":
            raise ValidationFailed("unknown record type")
        pep.enforce(
            SERVICE,
            ctx,
            subject_token=None,
            subject_programs=None,
            attributes=[],
            action="delete",
            purpose="retention_processing",
        )
        with session_for(SERVICE, Base) as s:
            e = s.get(Enrollment, body.record_ref)
            if e is None:
                return {"result": "already_absent"}
            if body.action == "archive":
                e.archived_at = _now()
                e.status = "archived"
                return {"result": "archived"}
            if body.action == "delete":
                s.delete(e)
                return {"result": "deleted"}
            raise ValidationFailed(f"unsupported end action '{body.action}'")

    return app
