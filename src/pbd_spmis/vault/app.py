"""Identity Vault and tokenization service.

The vault is the only store that holds direct identifiers. Everything else in the system refers
to a person by an opaque ``person_token`` or by a program-specific ``program_person_id``. Only
this service can map between them, and every resolution is a policy decision plus an audit event.

Endpoints
---------
POST /v1/identities                      identity proofing: create or find a person token
GET  /v1/identities/{token}/status       verified-or-not assertion, no identifiers
POST /v1/program-identifiers             issue a program-specific identifier
POST /v1/links/lookup                    program id -> person token (service roles only)
POST /v1/resolve                         release direct identifiers under policy (token_resolution
                                         for services, emergency_protection for break-glass)
POST /v1/internal/retention              retention engine callback
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel, Field
from sqlalchemy import LargeBinary, String, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..catalog.loader import get_catalog
from ..common import pep
from ..common.audit_client import emit
from ..common.clients import client
from ..common.context import RequestContext, request_context, service_context
from ..common.crypto import FieldCipher, LocalKeyProvider, blind_index
from ..common.db import session_for
from ..common.errors import NotFound, PolicyDenied, ValidationFailed, install_error_handlers
from ..common.ids import person_token as new_person_token
from ..common.ids import program_person_id as new_program_person_id
from ..common.retention_hook import schedule
from ..config import get_settings

SERVICE = "vault"
IDENTITY_FIELDS = ("national_id", "name", "contact")


class Base(DeclarativeBase):
    pass


class Identity(Base):
    __tablename__ = "identities"

    person_token: Mapped[str] = mapped_column(String(16), primary_key=True)
    national_id_idx: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_id: Mapped[str] = mapped_column(String(64))
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    national_id_ct: Mapped[bytes] = mapped_column(LargeBinary)
    name_ct: Mapped[bytes] = mapped_column(LargeBinary)
    contact_ct: Mapped[bytes] = mapped_column(LargeBinary)
    proofing_reference: Mapped[str] = mapped_column(String(128), default="")
    proofing_status: Mapped[str] = mapped_column(String(32), default="verified")
    created_at: Mapped[str] = mapped_column(String(40))
    deleted_at: Mapped[str | None] = mapped_column(String(40), default=None)


class ProgramLink(Base):
    __tablename__ = "program_links"
    __table_args__ = (UniqueConstraint("program", "person_token", name="uq_program_person"),)

    program_person_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    program: Mapped[str] = mapped_column(String(64), index=True)
    person_token: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[str] = mapped_column(String(40))


class IdentityIn(BaseModel):
    national_id: str = Field(min_length=3, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    contact: str = Field(default="", max_length=200)
    proofing_reference: str = Field(default="", max_length=128)


class ProgramIdentifierIn(BaseModel):
    person_token: str
    program: str


class LinkLookupIn(BaseModel):
    program_person_id: str


class ResolveIn(BaseModel):
    person_token: str | None = None
    program_person_id: str | None = None
    attributes: list[str] = Field(default_factory=lambda: ["national_id"])


class RetentionIn(BaseModel):
    record_type: str
    record_ref: str
    action: str


def _provider() -> LocalKeyProvider:
    return LocalKeyProvider.from_env(get_settings().vault_master_key)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def subject_programs(ctx: RequestContext, person_token: str) -> list[str]:
    """Programs the subject is related to: vault links plus registry relationships."""
    programs: set[str] = set()
    with session_for(SERVICE, Base) as s:
        for (p,) in s.execute(
            select(ProgramLink.program).where(ProgramLink.person_token == person_token)
        ).all():
            programs.add(p)
    with contextlib.suppress(Exception):  # enrichment is best-effort; the policy still gates
        resp = client("registry", caller=SERVICE).get(
            f"/v1/persons/{person_token}/relationships", headers={"X-Correlation-ID": ctx.correlation_id}
        )
        if resp.status_code == 200:
            programs.update(resp.json().get("programs", []))
    return sorted(programs)


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Identity Vault", version="0.1.0")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "key_id": _provider().key_id}

    @app.post("/v1/identities", status_code=201)
    def create_identity(
        body: IdentityIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=None,
            subject_programs=None,
            attributes=list(IDENTITY_FIELDS),
            action="write",
            purpose="identity_proofing",
        )
        idx = blind_index(get_settings().index_hmac_key, body.national_id)
        with session_for(SERVICE, Base) as s:
            existing = s.execute(select(Identity).where(Identity.national_id_idx == idx)).scalar_one_or_none()
            if existing and not existing.deleted_at:
                pep.attach_obligations(response, decision)
                response.status_code = 200
                emit(
                    SERVICE,
                    ctx,
                    "identity_matched",
                    outcome="existing",
                    subject_token=existing.person_token,
                    decision_id=decision.decision_id,
                )
                return {"person_token": existing.person_token, "created": False}
            cipher = FieldCipher.new(_provider())
            token = new_person_token()
            ident = Identity(
                person_token=token,
                national_id_idx=idx,
                key_id=cipher.key_id,
                wrapped_dek=cipher.wrapped_dek,
                national_id_ct=cipher.encrypt("national_id", body.national_id),
                name_ct=cipher.encrypt("name", body.name),
                contact_ct=cipher.encrypt("contact", body.contact),
                proofing_reference=body.proofing_reference,
                created_at=_now(),
            )
            s.add(ident)
        schedule(SERVICE, ctx, "identity_vault.identity", token, ctx.program)
        emit(
            SERVICE,
            ctx,
            "identity_created",
            outcome="created",
            subject_token=token,
            decision_id=decision.decision_id,
            attributes=list(IDENTITY_FIELDS),
        )
        pep.attach_obligations(response, decision)
        return {"person_token": token, "created": True}

    @app.get("/v1/identities/{person_token}/status")
    def identity_status(
        person_token: str, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=person_token,
            subject_programs=subject_programs(ctx, person_token),
            attributes=["national_id"],
            purpose="identity_proofing",
        )
        with session_for(SERVICE, Base) as s:
            ident = s.get(Identity, person_token)
            if ident is None or ident.deleted_at:
                raise NotFound("unknown person token")
            status = ident.proofing_status
            ref = ident.proofing_reference
        pep.attach_obligations(response, decision)
        return {"person_token": person_token, "verified": status == "verified", "proofing_reference": ref}

    @app.post("/v1/program-identifiers", status_code=201)
    def program_identifier(
        body: ProgramIdentifierIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        cat = get_catalog()
        if body.program not in cat.programs:
            raise ValidationFailed(f"unknown program '{body.program}'", code="unknown_program")
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=body.person_token,
            subject_programs=sorted(set(subject_programs(ctx, body.person_token)) | {body.program}),
            attributes=[],
            action="write",
            purpose="enrollment",
            program=body.program,
        )
        with session_for(SERVICE, Base) as s:
            if s.get(Identity, body.person_token) is None:
                raise NotFound("unknown person token")
            link = s.execute(
                select(ProgramLink).where(
                    ProgramLink.program == body.program, ProgramLink.person_token == body.person_token
                )
            ).scalar_one_or_none()
            if link:
                response.status_code = 200
                pep.attach_obligations(response, decision)
                return {"program_person_id": link.program_person_id, "created": False}
            ppid = new_program_person_id(cat.program_code(body.program))
            s.add(
                ProgramLink(
                    program_person_id=ppid,
                    program=body.program,
                    person_token=body.person_token,
                    created_at=_now(),
                )
            )
        emit(
            SERVICE,
            ctx,
            "program_identifier_issued",
            outcome="created",
            subject_token=body.person_token,
            decision_id=decision.decision_id,
            program=body.program,
        )
        pep.attach_obligations(response, decision)
        return {"program_person_id": ppid, "created": True}

    @app.post("/v1/links/lookup")
    def link_lookup(body: LinkLookupIn, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        if not ctx.actor.is_service:
            raise PolicyDenied("link lookup is restricted to service identities", code="service_only")
        with session_for(SERVICE, Base) as s:
            link = s.get(ProgramLink, body.program_person_id)
            if link is None:
                raise NotFound("unknown program identifier")
            out = {"person_token": link.person_token, "program": link.program}
        emit(
            SERVICE,
            ctx,
            "link_lookup",
            outcome="allow",
            subject_token=out["person_token"],
            program=out["program"],
            details={"program_person_id": body.program_person_id},
        )
        return out

    @app.post("/v1/resolve")
    def resolve(
        body: ResolveIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        unknown = [a for a in body.attributes if a not in IDENTITY_FIELDS]
        if unknown:
            raise ValidationFailed(f"vault does not hold {unknown}", code="unknown_attribute")
        token = body.person_token
        if token is None and body.program_person_id:
            with session_for(SERVICE, Base) as s:
                link = s.get(ProgramLink, body.program_person_id)
                if link is None:
                    raise NotFound("unknown program identifier")
                token = link.person_token
        if token is None:
            raise ValidationFailed("person_token or program_person_id required")
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=token,
            subject_programs=subject_programs(ctx, token),
            attributes=body.attributes,
        )
        with session_for(SERVICE, Base) as s:
            ident = s.get(Identity, token)
            if ident is None or ident.deleted_at:
                raise NotFound("unknown person token")
            cipher = FieldCipher.open(_provider(), ident.wrapped_dek)
            raw = {
                "national_id": cipher.decrypt("national_id", ident.national_id_ct),
                "name": cipher.decrypt("name", ident.name_ct),
                "contact": cipher.decrypt("contact", ident.contact_ct),
            }
        released = pep.apply_release(raw, decision, program=ctx.program)
        emit(
            SERVICE,
            ctx,
            "token_resolved",
            outcome="allow",
            subject_token=token,
            attributes=decision.released(),
            decision_id=decision.decision_id,
            obligations=decision.obligations,
            details={"release": decision.release, "break_glass_grant": ctx.break_glass_grant},
        )
        pep.attach_obligations(response, decision)
        return {"person_token": token, "attributes": released, "decision_id": decision.decision_id}

    @app.post("/v1/internal/retention")
    def retention(body: RetentionIn, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
        if body.record_type != "identity_vault.identity":
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
            ident = s.get(Identity, body.record_ref)
            if ident is None:
                return {"result": "already_absent"}
            if body.action == "delete":
                # Crypto-shred: drop the wrapped key so ciphertexts become unrecoverable, then
                # remove the row. Program links are kept so historical program ids stay unique.
                s.delete(ident)
                result = "deleted"
            else:
                raise ValidationFailed(f"unsupported end action '{body.action}' for identities")
        return {"result": result}

    return app
