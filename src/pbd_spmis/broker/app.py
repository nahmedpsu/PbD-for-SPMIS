"""Privacy-preserving data exchange broker ("query, do not copy").

For each requested check the broker:

1. maps the check to the catalogue attribute that governs it and asks the PDP whether the
   caller's purpose may receive that attribute at all (a ``deny`` release stops the check);
2. serves a cached assertion if one is still within the sharing matrix's TTL;
3. otherwise resolves the identifier *just in time* from the vault under the
   ``external_verification`` purpose, queries the adapter, filters the answer through the
   response schema from the sharing matrix, caches the filtered assertion, and records an
   ``external_disclosure`` audit event that names the source and the keys returned.

The resolved identifier never leaves the request scope and is never written anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..catalog.loader import get_catalog
from ..common import pep
from ..common.audit_client import emit
from ..common.clients import client
from ..common.context import RequestContext, request_context
from ..common.db import session_for
from ..common.errors import NotFound, UpstreamUnavailable, ValidationFailed, install_error_handlers
from ..common.ids import opaque
from .adapters.base import adapter_for, schema_filter

SERVICE = "broker"


class Base(DeclarativeBase):
    pass


class VerificationCache(Base):
    __tablename__ = "verification_cache"
    __table_args__ = (UniqueConstraint("person_token", "program", "check_name", name="uq_cache"),)

    cache_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    person_token: Mapped[str] = mapped_column(String(16), index=True)
    program: Mapped[str] = mapped_column(String(64))
    check_name: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verified_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40), index=True)


class VerifyIn(BaseModel):
    person_token: str
    checks: list[str] = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class RetentionIn(BaseModel):
    record_type: str
    record_ref: str
    action: str


def _now() -> datetime:
    return datetime.now(UTC)


def _find_source(cat: Any, check: str) -> tuple[str, dict[str, Any]]:
    for source, spec in cat.sharing["sources"].items():
        if check in spec["checks"]:
            return source, spec
    raise ValidationFailed(f"no authoritative source is registered for check '{check}'", code="unknown_check")


def _params_for(cat: Any, check: str, program: str, user_params: dict[str, Any]) -> dict[str, Any]:
    prog = cat.programs[program]
    params: dict[str, Any] = {}
    if check == "income_threshold":
        params["threshold"] = prog.get("income_threshold")
    if check == "income_band":
        params["bands"] = cat.attributes["income"].get("bands", [])
    if check == "disability_eligibility":
        params["eligible_statuses"] = prog.get("eligible_disability_statuses", [])
    if check == "residence":
        params["district"] = user_params.get("district")
    return params


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Data Exchange Broker", version="0.2.0")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/verify")
    def verify(
        body: VerifyIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        cat = get_catalog()
        if ctx.program not in cat.programs:
            raise ValidationFailed("unknown program", code="unknown_program")
        rel = client("registry", caller=SERVICE).get(
            f"/v1/persons/{body.person_token}/relationships", headers={"X-Correlation-ID": ctx.correlation_id}
        )
        if rel.status_code != 200:
            raise NotFound("person is not registered")
        subject_programs = rel.json()["programs"]

        results: dict[str, Any] = {}
        decision_ids: list[str] = []
        resolved_identifier: str | None = None
        for check in body.checks:
            attr = cat.sharing["checks"].get(check)
            if attr is None:
                raise ValidationFailed(f"unknown check '{check}'", code="unknown_check")
            source, spec = _find_source(cat, check)
            check_spec = spec["checks"][check]
            if ctx.purpose not in check_spec["purposes"]:
                results[check] = {
                    "status": "denied",
                    "reason": "PURPOSE_NOT_IN_SHARING_MATRIX",
                    "source": source,
                }
                continue
            decision = pep.decide(
                SERVICE,
                ctx,
                subject_token=body.person_token,
                subject_programs=subject_programs,
                attributes=[attr],
            )
            decision_ids.append(decision.decision_id)
            emit(
                SERVICE,
                ctx,
                "access_decision",
                outcome="allow" if decision.allow else "deny",
                subject_token=body.person_token,
                attributes=[attr],
                decision_id=decision.decision_id,
                reason_codes=decision.reason_codes,
                details={"check": check, "source": source},
            )
            if not decision.allow or decision.release.get(attr, "deny") == "deny":
                results[check] = {
                    "status": "denied",
                    "reason": decision.reason_codes[0]
                    if not decision.allow
                    else decision.attribute_reasons.get(attr, "DENIED"),
                    "source": source,
                    "decision_id": decision.decision_id,
                }
                continue

            cached = _cached(body.person_token, ctx.program, check)
            if cached:
                results[check] = {
                    **cached["result"],
                    "source": cached["source"],
                    "verified_at": cached["verified_at"],
                    "cached": True,
                    "decision_id": decision.decision_id,
                }
                continue

            if resolved_identifier is None:
                resolved_identifier = _resolve_identifier(ctx, body.person_token, check_spec["identifier"])
            adapter = adapter_for(spec["adapter"])
            params = _params_for(cat, check, ctx.program, body.params)
            answer = adapter.query(check, resolved_identifier, params)
            filtered = schema_filter(answer, check_spec["response_schema"])
            status = answer.get("status", "ok")
            verified_at = _now()
            ttl = timedelta(hours=float(check_spec.get("cache_ttl_hours", 24)))
            if status == "ok":
                _store_cache(
                    body.person_token, ctx.program, check, source, filtered, verified_at, verified_at + ttl
                )
            emit(
                SERVICE,
                ctx,
                "external_disclosure",
                outcome=status,
                subject_token=body.person_token,
                attributes=[attr],
                decision_id=decision.decision_id,
                obligations=decision.obligations,
                details={
                    "source": source,
                    "check": check,
                    "keys_returned": sorted(filtered),
                    "agreement": spec.get("agreement", ""),
                },
            )
            results[check] = {
                **filtered,
                "status": status,
                "source": source,
                "verified_at": verified_at.isoformat(),
                "cached": False,
                "decision_id": decision.decision_id,
            }
        del resolved_identifier
        response.headers["X-Correlation-ID"] = ctx.correlation_id
        return {
            "person_token": body.person_token,
            "program": ctx.program,
            "results": results,
            "decision_ids": decision_ids,
        }

    @app.post("/v1/internal/retention")
    def retention(body: RetentionIn, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
        if body.record_type != "broker.verification_cache":
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
            rows = s.execute(
                select(VerificationCache).where(VerificationCache.person_token == body.record_ref)
            ).scalars()
            n = 0
            for r in rows:
                s.delete(r)
                n += 1
        return {"result": "deleted", "count": n}

    return app


def _resolve_identifier(ctx: RequestContext, person_token: str, identifier: str) -> str:
    resp = client("vault", caller=SERVICE).post(
        "/v1/resolve",
        headers=ctx.forward_headers(purpose="external_verification"),
        json={"person_token": person_token, "attributes": [identifier]},
    )
    if resp.status_code != 200:
        raise UpstreamUnavailable(
            f"vault refused identifier resolution: {resp.status_code} {resp.text[:120]}"
        )
    value = resp.json()["attributes"].get(identifier)
    if not isinstance(value, str):
        raise UpstreamUnavailable("vault did not release the identifier exactly")
    return value


def _cached(person_token: str, program: str, check: str) -> dict[str, Any] | None:
    with session_for(SERVICE, Base) as s:
        row = s.execute(
            select(VerificationCache).where(
                VerificationCache.person_token == person_token,
                VerificationCache.program == program,
                VerificationCache.check_name == check,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if datetime.fromisoformat(row.expires_at) < _now():
            s.delete(row)
            return None
        return {"result": row.result, "source": row.source, "verified_at": row.verified_at}


def _store_cache(
    person_token: str,
    program: str,
    check: str,
    source: str,
    result: dict[str, Any],
    verified_at: datetime,
    expires_at: datetime,
) -> None:
    with session_for(SERVICE, Base) as s:
        row = s.execute(
            select(VerificationCache).where(
                VerificationCache.person_token == person_token,
                VerificationCache.program == program,
                VerificationCache.check_name == check,
            )
        ).scalar_one_or_none()
        if row is None:
            row = VerificationCache(
                cache_id=opaque("VC", 10),
                person_token=person_token,
                program=program,
                check_name=check,
                source=source,
                result=result,
                verified_at=verified_at.isoformat(),
                expires_at=expires_at.isoformat(),
            )
            s.add(row)
        else:
            row.result, row.source = result, source
            row.verified_at, row.expires_at = verified_at.isoformat(), expires_at.isoformat()
