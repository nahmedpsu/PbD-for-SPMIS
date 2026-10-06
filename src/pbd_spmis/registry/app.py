"""Social Registry service.

Holds household and socioeconomic data keyed by opaque tokens. It never stores a name, a
national id or a payment instrument. Reads go through the PEP, which turns raw values into what
the purpose permits: a case worker verifying eligibility receives ``income: {"below_threshold":
true}`` and ``address: {"district": "North"}``, not the salary and the street.

Endpoints
---------
POST /v1/persons                              register (purpose: registration)
GET  /v1/persons/{token}?attributes=a,b       purpose-bound read
PUT  /v1/persons/{token}                      update (purpose: registration / case_management)
GET  /v1/persons/{token}/relationships        programs the person is related to (services only)
POST /v1/persons/{token}/relationships        add a program relationship (services only)
GET  /v1/export?attributes=a,b                de-identified extract (purpose: analytics_reporting)
POST /v1/internal/retention                   retention callback (anonymize / delete)
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Float, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..catalog.loader import get_catalog
from ..common import pep
from ..common.audit_client import emit
from ..common.context import RequestContext, request_context, service_context
from ..common.db import session_for
from ..common.errors import NotFound, PolicyDenied, ValidationFailed, install_error_handlers
from ..common.ids import household_token as new_household_token
from ..common.retention_hook import schedule

SERVICE = "registry"
REGISTRY_ATTRIBUTES = (
    "income",
    "household_size",
    "disability_status",
    "employment_status",
    "vulnerability_attributes",
    "address",
)


class Base(DeclarativeBase):
    pass


class Household(Base):
    __tablename__ = "households"
    household_token: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40))


class Person(Base):
    __tablename__ = "persons"
    person_token: Mapped[str] = mapped_column(String(16), primary_key=True)
    household_token: Mapped[str] = mapped_column(String(16), index=True)
    income: Mapped[float | None] = mapped_column(Float, nullable=True)
    household_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    disability_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    employment_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vulnerability_attributes: Mapped[list[str]] = mapped_column(JSON, default=list)
    address: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    program_relationships: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))
    anonymized_at: Mapped[str | None] = mapped_column(String(40), default=None)


class Address(BaseModel):
    street: str = ""
    district: str = ""
    region: str = ""


class PersonIn(BaseModel):
    person_token: str
    household_token: str | None = None
    income: float | None = None
    household_size: int | None = Field(default=None, ge=1)
    disability_status: str | None = None
    employment_status: str | None = None
    vulnerability_attributes: list[str] = Field(default_factory=list)
    address: Address = Field(default_factory=Address)


class PersonUpdate(BaseModel):
    income: float | None = None
    household_size: int | None = Field(default=None, ge=1)
    disability_status: str | None = None
    employment_status: str | None = None
    vulnerability_attributes: list[str] | None = None
    address: Address | None = None


class RelationshipIn(BaseModel):
    program: str


class RetentionIn(BaseModel):
    record_type: str
    record_ref: str
    action: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _raw(p: Person) -> dict[str, Any]:
    return {
        "income": p.income,
        "household_size": p.household_size,
        "disability_status": p.disability_status,
        "employment_status": p.employment_status,
        "vulnerability_attributes": p.vulnerability_attributes,
        "address": p.address,
    }


def _parse_attributes(attributes: str | None) -> list[str]:
    if not attributes:
        return list(REGISTRY_ATTRIBUTES)
    requested = [a.strip() for a in attributes.split(",") if a.strip()]
    unknown = [a for a in requested if a not in REGISTRY_ATTRIBUTES]
    if unknown:
        raise ValidationFailed(f"registry does not hold {unknown}", code="unknown_attribute")
    return requested


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Social Registry", version="0.1.0")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/persons", status_code=201)
    def register(
        body: PersonIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        provided = [a for a in REGISTRY_ATTRIBUTES if getattr(body, a) not in (None, [], Address())]
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=body.person_token,
            subject_programs=[ctx.program],
            attributes=provided,
            action="write",
            purpose="registration",
        )
        with session_for(SERVICE, Base) as s:
            if s.get(Person, body.person_token):
                raise ValidationFailed("person already registered", code="already_registered")
            htoken = body.household_token or new_household_token()
            if s.get(Household, htoken) is None:
                s.add(Household(household_token=htoken, created_at=_now()))
            s.add(
                Person(
                    person_token=body.person_token,
                    household_token=htoken,
                    income=body.income,
                    household_size=body.household_size,
                    disability_status=body.disability_status,
                    employment_status=body.employment_status,
                    vulnerability_attributes=body.vulnerability_attributes,
                    address=body.address.model_dump(),
                    program_relationships=[ctx.program],
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        schedule(SERVICE, ctx, "social_registry.person", body.person_token, ctx.program)
        emit(
            SERVICE,
            ctx,
            "person_registered",
            outcome="created",
            subject_token=body.person_token,
            attributes=provided,
            decision_id=decision.decision_id,
        )
        pep.attach_obligations(response, decision)
        return {
            "person_token": body.person_token,
            "household_token": htoken,
            "program_relationships": [ctx.program],
        }

    @app.get("/v1/persons/{person_token}")
    def read_person(
        person_token: str,
        response: Response,
        ctx: RequestContext = Depends(request_context),
        attributes: str | None = Query(default=None, description="comma-separated attribute names"),
    ) -> dict[str, Any]:
        requested = _parse_attributes(attributes)
        with session_for(SERVICE, Base) as s:
            p = s.get(Person, person_token)
            if p is None:
                raise NotFound("unknown person token")
            raw, relationships = _raw(p), list(p.program_relationships)
        decision = pep.enforce(
            SERVICE, ctx, subject_token=person_token, subject_programs=relationships, attributes=requested
        )
        pep.attach_obligations(response, decision)
        return {
            "person_token": person_token,
            "attributes": pep.apply_release(raw, decision, program=ctx.program),
            "release": decision.release,
            "decision_id": decision.decision_id,
            "obligations": decision.obligations,
        }

    @app.put("/v1/persons/{person_token}")
    def update_person(
        person_token: str,
        body: PersonUpdate,
        response: Response,
        ctx: RequestContext = Depends(request_context),
    ) -> dict[str, Any]:
        changes = {k: v for k, v in body.model_dump(exclude_none=True).items()}
        if not changes:
            raise ValidationFailed("no changes supplied")
        with session_for(SERVICE, Base) as s:
            p = s.get(Person, person_token)
            if p is None:
                raise NotFound("unknown person token")
            relationships = list(p.program_relationships)
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=person_token,
            subject_programs=relationships,
            attributes=list(changes),
            action="write",
        )
        with session_for(SERVICE, Base) as s:
            p = s.get(Person, person_token)
            assert p is not None
            for k, v in changes.items():
                setattr(p, k, v)
            p.updated_at = _now()
        emit(
            SERVICE,
            ctx,
            "person_updated",
            outcome="updated",
            subject_token=person_token,
            attributes=list(changes),
            decision_id=decision.decision_id,
        )
        pep.attach_obligations(response, decision)
        return {"person_token": person_token, "updated": sorted(changes)}

    def _require_service(ctx: RequestContext) -> None:
        if not ctx.actor.is_service:
            raise PolicyDenied("relationship data is only available to services", code="service_only")

    @app.get("/v1/persons/{person_token}/relationships")
    def relationships(person_token: str, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        _require_service(ctx)
        with session_for(SERVICE, Base) as s:
            p = s.get(Person, person_token)
            if p is None:
                raise NotFound("unknown person token")
            return {"person_token": person_token, "programs": list(p.program_relationships)}

    @app.post("/v1/persons/{person_token}/relationships")
    def add_relationship(
        person_token: str, body: RelationshipIn, ctx: RequestContext = Depends(service_context)
    ) -> dict[str, Any]:
        _require_service(ctx)
        if body.program not in get_catalog().programs:
            raise ValidationFailed("unknown program", code="unknown_program")
        with session_for(SERVICE, Base) as s:
            p = s.get(Person, person_token)
            if p is None:
                raise NotFound("unknown person token")
            rel = list(p.program_relationships)
            if body.program not in rel:
                rel.append(body.program)
                p.program_relationships = rel
                p.updated_at = _now()
            return {"person_token": person_token, "programs": rel}

    @app.get("/v1/export")
    def export(
        response: Response,
        ctx: RequestContext = Depends(request_context),
        attributes: str | None = Query(default=None),
    ) -> dict[str, Any]:
        requested = _parse_attributes(attributes)
        decision = pep.enforce(
            SERVICE, ctx, subject_token=None, subject_programs=None, attributes=requested, action="export"
        )
        k = _k_from_obligations(decision.obligations)
        with session_for(SERVICE, Base) as s:
            rows = [
                pep.apply_release(_raw(p), decision, program=ctx.program)
                for p in s.execute(select(Person).where(Person.anonymized_at.is_(None))).scalars()
                if ctx.program in p.program_relationships
            ]
        suppressed = 0
        if k > 1:
            keys = Counter(_row_key(r) for r in rows)
            kept = [r for r in rows if keys[_row_key(r)] >= k]
            suppressed = len(rows) - len(kept)
            rows = kept
        emit(
            SERVICE,
            ctx,
            "export",
            outcome="allow",
            attributes=decision.released(),
            decision_id=decision.decision_id,
            obligations=decision.obligations,
            details={"rows": len(rows), "suppressed": suppressed, "k": k},
        )
        pep.attach_obligations(response, decision)
        return {
            "rows": rows,
            "count": len(rows),
            "suppressed_for_k_anonymity": suppressed,
            "k": k,
            "release": decision.release,
            "decision_id": decision.decision_id,
        }

    @app.post("/v1/internal/retention")
    def retention(body: RetentionIn, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
        if body.record_type != "social_registry.person":
            raise ValidationFailed("unknown record type")
        pep.enforce(
            SERVICE,
            ctx,
            subject_token=body.record_ref,
            subject_programs=None,
            attributes=[],
            action="delete",
            purpose="retention_processing",
        )
        with session_for(SERVICE, Base) as s:
            p = s.get(Person, body.record_ref)
            if p is None:
                return {"result": "already_absent"}
            if body.action == "anonymize":
                p.income = None
                p.disability_status = None
                p.employment_status = None
                p.vulnerability_attributes = []
                p.address = {"region": p.address.get("region", "")}
                p.anonymized_at = _now()
                p.updated_at = _now()
                return {"result": "anonymized"}
            if body.action == "delete":
                s.delete(p)
                return {"result": "deleted"}
            raise ValidationFailed(f"unsupported end action '{body.action}'")

    return app


def _k_from_obligations(obligations: list[str]) -> int:
    for o in obligations:
        if o.startswith("k_anonymity:"):
            try:
                return int(o.split(":", 1)[1])
            except ValueError:
                return 1
    return 1


def _row_key(row: dict[str, Any]) -> str:
    import json

    return json.dumps(row, sort_keys=True, default=str)
