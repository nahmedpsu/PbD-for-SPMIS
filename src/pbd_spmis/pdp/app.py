"""Policy Decision Point service.

POST /v1/decisions   evaluate a policy input and return a decision document
GET  /v1/policy      describe the loaded catalogue version and engine
GET  /healthz

The PDP never sees personal data: inputs are tokens, roles, purposes and attribute *names*.
Every decision is written to the audit store with its policy version and reason codes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from ..catalog.loader import get_catalog
from ..common.audit_client import emit
from ..common.context import RequestContext, service_context
from ..common.errors import UpstreamUnavailable, install_error_handlers
from ..common.ids import decision_id as new_decision_id
from ..config import get_settings
from . import engine
from .opa_engine import OpaEngine

SERVICE = "pdp"


class ActorIn(BaseModel):
    id: str
    role: str
    agency: str = ""
    office: str = ""
    programs: list[str] = Field(default_factory=list)
    cases: list[str] = Field(default_factory=list)
    amr: list[str] = Field(default_factory=lambda: ["pwd"])
    is_service: bool = False


class SubjectIn(BaseModel):
    person_token: str | None = None
    program_relationship: list[str] = Field(default_factory=list)


class DecisionRequest(BaseModel):
    actor: ActorIn
    subject: SubjectIn = Field(default_factory=SubjectIn)
    program: str
    purpose: str
    action: str = "read"
    attributes: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class DecisionResponse(BaseModel):
    decision_id: str
    allow: bool
    policy_version: str
    engine: str
    release: dict[str, str]
    obligations: list[str]
    reason_codes: list[str]
    attribute_reasons: dict[str, str]
    evaluated_at: str


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Policy Decision Point", version="0.2.1")
    install_error_handlers(app)
    settings = get_settings()
    opa = OpaEngine(settings.opa_url) if settings.policy_engine == "opa" else None

    def _evaluate(inp: dict[str, Any]) -> dict[str, Any]:
        if opa is not None:
            return opa.evaluate(inp)
        return engine.evaluate(get_catalog().data, inp)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        ok = True if opa is None else opa.healthy()
        return {"status": "ok" if ok else "degraded", "engine": settings.policy_engine}

    @app.get("/v1/policy")
    def policy_info(ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        cat = get_catalog()
        return {
            "policy_version": cat.version,
            "engine": settings.policy_engine,
            "purposes": sorted(cat.purposes),
            "programs": sorted(cat.programs),
            "attributes": sorted(cat.attributes),
            "fail_closed": settings.fail_closed,
        }

    @app.get("/v1/catalog")
    def catalog_view(ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        """Non-sensitive catalogue view for SDKs and adapters (bands, thresholds, classes)."""
        return get_catalog().view()

    @app.post("/v1/decisions", response_model=DecisionResponse)
    def decide(req: DecisionRequest, ctx: RequestContext = Depends(service_context)) -> DecisionResponse:
        inp = req.model_dump()
        try:
            result = _evaluate(inp)
        except UpstreamUnavailable:
            if settings.fail_closed:
                raise
            raise
        did = new_decision_id()
        emit(
            SERVICE,
            ctx,
            "policy_decision",
            outcome="allow" if result["allow"] else "deny",
            subject_token=req.subject.person_token,
            attributes=req.attributes,
            decision_id=did,
            reason_codes=result["reason_codes"],
            obligations=result["obligations"],
            purpose=req.purpose,
            program=req.program,
            details={
                "requesting_actor": req.actor.id,
                "requesting_role": req.actor.role,
                "action": req.action,
                "release": result["release"],
                "policy_version": result["policy_version"],
            },
        )
        return DecisionResponse(
            decision_id=did,
            engine=settings.policy_engine,
            evaluated_at=datetime.now(UTC).isoformat(),
            **result,
        )

    return app
