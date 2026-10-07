"""Eligibility verification service.

Implements the API contract from the guide's Appendix B. The service asks the broker for
assertions, evaluates the program's rules from the catalogue, and stores the determination with
its evidence and provenance (which source answered, when, under which decision). It never stores
the underlying external records.

POST /v1/eligibility/verify
GET  /v1/eligibility/determinations/{id}
POST /v1/internal/retention
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..catalog.loader import get_catalog
from ..common import pep
from ..common.audit_client import emit
from ..common.clients import client
from ..common.context import RequestContext, request_context
from ..common.db import session_for
from ..common.errors import NotFound, UpstreamUnavailable, ValidationFailed, install_error_handlers
from ..common.ids import opaque
from ..common.retention_hook import schedule

SERVICE = "eligibility"


class Base(DeclarativeBase):
    pass


class Determination(Base):
    __tablename__ = "determinations"
    determination_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    person_token: Mapped[str] = mapped_column(String(16), index=True)
    program: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rule_version: Mapped[str] = mapped_column(String(32))
    decision_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(String(40))


class VerifyIn(BaseModel):
    person_token: str
    checks: list[str] | None = Field(default=None, description="defaults to the program's eligibility rules")


class RetentionIn(BaseModel):
    record_type: str
    record_ref: str
    action: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def evaluate_rules(rules: dict[str, Any], results: dict[str, Any]) -> str:
    """Return eligible / ineligible / incomplete from check results and the program's rules."""

    def value(check: str) -> bool | None:
        r = results.get(check) or {}
        if r.get("status") not in ("ok", None) and "cached" not in r:
            return None
        for key in ("met", "eligible", "verified", "match"):
            if key in r:
                return bool(r[key])
        return None

    all_of = [value(c) for c in rules.get("all_of", [])]
    any_of = [value(c) for c in rules.get("any_of", [])]
    if any(v is None for v in all_of) or (any_of and all(v is None for v in any_of)):
        return "incomplete"
    if all(all_of) and (not any_of or any(any_of)):
        return "eligible"
    return "ineligible"


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Eligibility Service", version="0.2.1")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/eligibility/verify")
    def verify(
        body: VerifyIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        cat = get_catalog()
        if ctx.program not in cat.programs:
            raise ValidationFailed("unknown program", code="unknown_program")
        prog = cat.programs[ctx.program]
        rules = prog.get("eligibility", {})
        checks = body.checks or (rules.get("all_of", []) + rules.get("any_of", []))
        attrs = sorted({cat.sharing["checks"][c] for c in checks if c in cat.sharing["checks"]})
        rel = client("registry", caller=SERVICE).get(
            f"/v1/persons/{body.person_token}/relationships", headers={"X-Correlation-ID": ctx.correlation_id}
        )
        if rel.status_code != 200:
            raise NotFound("person is not registered")
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=body.person_token,
            subject_programs=rel.json()["programs"],
            attributes=attrs,
        )
        params: dict[str, Any] = {}
        if "residence" in checks:
            reg = client("registry", caller=SERVICE).get(
                f"/v1/persons/{body.person_token}",
                headers=ctx.forward_headers(),
                params={"attributes": "address"},
            )
            if reg.status_code == 200:
                params["district"] = (reg.json()["attributes"].get("address") or {}).get("district")
        broker = client("broker", caller=SERVICE).post(
            "/v1/verify",
            headers=ctx.forward_headers(),
            json={"person_token": body.person_token, "checks": checks, "params": params},
        )
        if broker.status_code != 200:
            raise UpstreamUnavailable(f"broker returned {broker.status_code}: {broker.text[:160]}")
        results = broker.json()["results"]
        outcome = (
            evaluate_rules(rules, results) if not body.checks else evaluate_rules({"all_of": checks}, results)
        )
        det_id = opaque("ED", 10)
        evidence = {c: {k: v for k, v in r.items() if k != "decision_id"} for c, r in results.items()}
        with session_for(SERVICE, Base) as s:
            s.add(
                Determination(
                    determination_id=det_id,
                    person_token=body.person_token,
                    program=ctx.program,
                    outcome=outcome,
                    evidence=evidence,
                    rule_version=cat.version,
                    decision_ids=[decision.decision_id, *broker.json()["decision_ids"]],
                    created_at=_now(),
                )
            )
        schedule(SERVICE, ctx, "eligibility.determination", det_id, ctx.program)
        emit(
            SERVICE,
            ctx,
            "eligibility_calculated",
            outcome=outcome,
            subject_token=body.person_token,
            decision_id=decision.decision_id,
            attributes=attrs,
            details={"determination_id": det_id, "checks": checks, "rule_version": cat.version},
        )
        pep.attach_obligations(response, decision)
        return {
            "result": {
                c: {
                    k: v
                    for k, v in r.items()
                    if k
                    in (
                        "met",
                        "eligible",
                        "verified",
                        "match",
                        "band",
                        "status",
                        "source",
                        "verified_at",
                        "cached",
                    )
                }
                for c, r in results.items()
            },
            "outcome": outcome,
            "determination_id": det_id,
            "decision_id": decision.decision_id,
            "rule_version": cat.version,
        }

    @app.get("/v1/eligibility/determinations/{determination_id}")
    def read_determination(
        determination_id: str, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            d = s.get(Determination, determination_id)
            if d is None:
                raise NotFound("unknown determination")
            raw = {
                "eligibility_outcome": {
                    "determination_id": d.determination_id,
                    "person_token": d.person_token,
                    "program": d.program,
                    "outcome": d.outcome,
                    "evidence": d.evidence,
                    "rule_version": d.rule_version,
                    "created_at": d.created_at,
                }
            }
            token, program = d.person_token, d.program
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=token,
            subject_programs=[program],
            attributes=["eligibility_outcome"],
            program=program,
        )
        pep.attach_obligations(response, decision)
        return {
            "determination_id": determination_id,
            "attributes": pep.apply_release(raw, decision, program=program),
            "decision_id": decision.decision_id,
        }

    @app.post("/v1/internal/retention")
    def retention(body: RetentionIn, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
        if body.record_type != "eligibility.determination":
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
            d = s.get(Determination, body.record_ref)
            if d is None:
                return {"result": "already_absent"}
            s.delete(d)
        return {"result": "deleted"}

    return app
