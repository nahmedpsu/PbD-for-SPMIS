"""Payment reference store and tokenized payment orchestration.

Business services see payment *tokens*, status, amount and exception reason. The account itself
is envelope-encrypted in this store and is decrypted only inside the provider adapter at the
moment of disbursement. Payment data is retained under finance rules, separately from case data.

POST /v1/instruments                      register a payment instrument -> payment token
POST /v1/instructions                     create an instruction from an enrollment entitlement
POST /v1/instructions/{id}/execute        execute through the provider adapter
POST /v1/instructions/{id}/reconcile      provider callback / reconciliation
GET  /v1/instructions/{id}                status view (no credentials)
POST /v1/internal/retention
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Response
from pydantic import BaseModel, Field
from sqlalchemy import Float, LargeBinary, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..common import pep
from ..common.audit_client import emit
from ..common.clients import client
from ..common.context import RequestContext, request_context
from ..common.crypto import FieldCipher, LocalKeyProvider
from ..common.db import session_for
from ..common.errors import NotFound, UpstreamUnavailable, ValidationFailed, install_error_handlers
from ..common.ids import opaque, payment_token
from ..common.retention_hook import schedule
from ..config import get_settings
from .provider import MockPaymentProvider

SERVICE = "payments"


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instruments"
    payment_token: Mapped[str] = mapped_column(String(16), primary_key=True)
    person_token: Mapped[str] = mapped_column(String(16), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    key_id: Mapped[str] = mapped_column(String(64))
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary)
    account_ct: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[str] = mapped_column(String(40))


class Instruction(Base):
    __tablename__ = "instructions"
    instruction_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    program_person_id: Mapped[str] = mapped_column(String(32), index=True)
    program: Mapped[str] = mapped_column(String(64))
    person_token: Mapped[str] = mapped_column(String(16), index=True)
    payment_token: Mapped[str] = mapped_column(String(16))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), index=True)
    provider_reference: Mapped[str | None] = mapped_column(String(64), default=None)
    exception_reason: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[str] = mapped_column(String(40))
    executed_at: Mapped[str | None] = mapped_column(String(40), default=None)
    archived_at: Mapped[str | None] = mapped_column(String(40), default=None)


class InstrumentIn(BaseModel):
    person_token: str
    provider: str = "mock_bank"
    account_number: str = Field(min_length=4, max_length=64)
    bank_code: str = Field(default="", max_length=32)


class InstructionIn(BaseModel):
    program_person_id: str
    amount: float | None = Field(default=None, gt=0)
    currency: str | None = None


class ReconcileIn(BaseModel):
    status: str = Field(pattern=r"^(settled|returned)$")
    provider_reference: str


class RetentionIn(BaseModel):
    record_type: str
    record_ref: str
    action: str


def _provider_key() -> LocalKeyProvider:
    return LocalKeyProvider.from_env(get_settings().vault_master_key)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _status_view(i: Instruction) -> dict[str, Any]:
    return {
        "status": i.status,
        "amount": i.amount,
        "currency": i.currency,
        "exception_reason": i.exception_reason,
        "provider_reference": i.provider_reference,
        "executed_at": i.executed_at,
    }


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Payments", version="0.1.0")
    install_error_handlers(app)
    provider = MockPaymentProvider()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/instruments", status_code=201)
    def register_instrument(
        body: InstrumentIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
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
            attributes=["bank_account"],
            action="write",
        )
        cipher = FieldCipher.new(_provider_key())
        token = payment_token()
        with session_for(SERVICE, Base) as s:
            s.add(
                Instrument(
                    payment_token=token,
                    person_token=body.person_token,
                    provider=body.provider,
                    key_id=cipher.key_id,
                    wrapped_dek=cipher.wrapped_dek,
                    account_ct=cipher.encrypt("bank_account", f"{body.bank_code}:{body.account_number}"),
                    created_at=_now(),
                )
            )
        schedule(SERVICE, ctx, "payment_reference.instrument", token, ctx.program)
        emit(
            SERVICE,
            ctx,
            "instrument_registered",
            outcome="created",
            subject_token=body.person_token,
            attributes=["bank_account"],
            decision_id=decision.decision_id,
            details={"payment_token": token},
        )
        pep.attach_obligations(response, decision)
        return {"payment_token": token}

    @app.post("/v1/instructions", status_code=201)
    def create_instruction(
        body: InstructionIn, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        fwd = ctx.forward_headers(purpose="payment_execution")
        link = client("vault", caller=SERVICE).post(
            "/v1/links/lookup", headers=fwd, json={"program_person_id": body.program_person_id}
        )
        if link.status_code != 200:
            raise NotFound("unknown program identifier")
        person_token, program = link.json()["person_token"], link.json()["program"]
        if program != ctx.program:
            raise ValidationFailed(
                "program identifier belongs to a different program", code="program_mismatch"
            )
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=person_token,
            subject_programs=[program],
            attributes=["entitlement", "bank_account"],
            action="write",
            purpose="payment_execution",
        )
        enr = client("program", caller=SERVICE).get(f"/v1/enrollments/{body.program_person_id}", headers=fwd)
        if enr.status_code != 200:
            raise UpstreamUnavailable(f"program store returned {enr.status_code}")
        attrs = enr.json()["attributes"]
        if attrs.get("enrollment_status") != "enrolled":
            raise ValidationFailed("person is not enrolled", code="not_enrolled")
        ent = attrs.get("entitlement") or {}
        amount = body.amount or float(ent.get("amount", 0))
        currency = body.currency or ent.get("currency", "XSP")
        if amount <= 0:
            raise ValidationFailed("no entitlement amount", code="no_entitlement")
        with session_for(SERVICE, Base) as s:
            inst = s.execute(
                select(Instrument).where(Instrument.person_token == person_token)
            ).scalar_one_or_none()
            if inst is None:
                raise ValidationFailed("no payment instrument registered", code="no_instrument")
            iid = opaque("PI", 10)
            s.add(
                Instruction(
                    instruction_id=iid,
                    program_person_id=body.program_person_id,
                    program=program,
                    person_token=person_token,
                    payment_token=inst.payment_token,
                    amount=amount,
                    currency=currency,
                    status="approved",
                    created_at=_now(),
                )
            )
        schedule(SERVICE, ctx, "payment_reference.instruction", iid, program)
        emit(
            SERVICE,
            ctx,
            "payment_instruction_created",
            outcome="approved",
            subject_token=person_token,
            decision_id=decision.decision_id,
            details={"instruction_id": iid, "amount": amount, "currency": currency},
        )
        pep.attach_obligations(response, decision)
        return {
            "instruction_id": iid,
            "status": "approved",
            "amount": amount,
            "currency": currency,
            "payment_token": inst.payment_token,
        }

    @app.post("/v1/instructions/{instruction_id}/execute")
    def execute(
        instruction_id: str, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            i = s.get(Instruction, instruction_id)
            if i is None:
                raise NotFound("unknown instruction")
            if i.status != "approved":
                raise ValidationFailed(f"instruction is {i.status}", code="invalid_state")
            person_token, program, ptoken, amount, currency = (
                i.person_token,
                i.program,
                i.payment_token,
                i.amount,
                i.currency,
            )
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=person_token,
            subject_programs=[program],
            attributes=["bank_account", "name"],
            purpose="payment_execution",
            program=program,
        )
        if decision.release.get("bank_account") != "token":
            raise ValidationFailed("caller may not execute payments", code="execution_not_permitted")
        # Beneficiary name for the provider: resolved just-in-time, never stored here.
        name_resp = client("vault", caller=SERVICE).post(
            "/v1/resolve",
            headers=ctx.forward_headers(purpose="token_resolution", program=program),
            json={"person_token": person_token, "attributes": ["name"]},
        )
        beneficiary_name = (
            name_resp.json()["attributes"].get("name", "") if name_resp.status_code == 200 else ""
        )
        with session_for(SERVICE, Base) as s:
            inst = s.get(Instrument, ptoken)
            if inst is None:
                raise ValidationFailed("instrument missing", code="no_instrument")
            account = FieldCipher.open(_provider_key(), inst.wrapped_dek).decrypt(
                "bank_account", inst.account_ct
            )
            result = provider.disburse(
                account=account,
                beneficiary_name=beneficiary_name,
                amount=amount,
                currency=currency,
                reference=instruction_id,
            )
            del account, beneficiary_name
            i = s.get(Instruction, instruction_id)
            assert i is not None
            i.status = "executed" if result["ok"] else "failed"
            i.provider_reference = result.get("reference")
            i.exception_reason = result.get("error")
            i.executed_at = _now()
            view = _status_view(i)
        emit(
            SERVICE,
            ctx,
            "payment_issued" if result["ok"] else "payment_failed",
            outcome=view["status"],
            subject_token=person_token,
            decision_id=decision.decision_id,
            attributes=["bank_account", "name"],
            details={
                "instruction_id": instruction_id,
                "provider_reference": view["provider_reference"],
                "exception_reason": view["exception_reason"],
            },
        )
        pep.attach_obligations(response, decision)
        return {"instruction_id": instruction_id, **view}

    @app.post("/v1/instructions/{instruction_id}/reconcile")
    def reconcile(
        instruction_id: str,
        body: ReconcileIn,
        response: Response,
        ctx: RequestContext = Depends(request_context),
    ) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            i = s.get(Instruction, instruction_id)
            if i is None:
                raise NotFound("unknown instruction")
            person_token, program = i.person_token, i.program
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=person_token,
            subject_programs=[program],
            attributes=["payment_status"],
            action="write",
            purpose="payment_execution",
            program=program,
        )
        with session_for(SERVICE, Base) as s:
            i = s.get(Instruction, instruction_id)
            assert i is not None
            if i.provider_reference != body.provider_reference:
                raise ValidationFailed("provider reference mismatch", code="reference_mismatch")
            i.status = body.status
            view = _status_view(i)
        emit(
            SERVICE,
            ctx,
            "payment_reconciled",
            outcome=body.status,
            subject_token=person_token,
            decision_id=decision.decision_id,
            details={"instruction_id": instruction_id},
        )
        pep.attach_obligations(response, decision)
        return {"instruction_id": instruction_id, **view}

    @app.get("/v1/instructions/{instruction_id}")
    def read_instruction(
        instruction_id: str, response: Response, ctx: RequestContext = Depends(request_context)
    ) -> dict[str, Any]:
        with session_for(SERVICE, Base) as s:
            i = s.get(Instruction, instruction_id)
            if i is None:
                raise NotFound("unknown instruction")
            raw = {"payment_status": _status_view(i), "bank_account": {"token": i.payment_token}}
            person_token, program = i.person_token, i.program
        decision = pep.enforce(
            SERVICE,
            ctx,
            subject_token=person_token,
            subject_programs=[program],
            attributes=["payment_status", "bank_account"],
            program=program,
        )
        pep.attach_obligations(response, decision)
        return {
            "instruction_id": instruction_id,
            "attributes": pep.apply_release(raw, decision, program=program),
            "decision_id": decision.decision_id,
        }

    @app.post("/v1/internal/retention")
    def retention(body: RetentionIn, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
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
            if body.record_type == "payment_reference.instruction":
                i = s.get(Instruction, body.record_ref)
                if i is None:
                    return {"result": "already_absent"}
                if body.action == "archive":
                    i.archived_at = _now()
                    return {"result": "archived"}
                s.delete(i)
                return {"result": "deleted"}
            if body.record_type == "payment_reference.instrument":
                inst = s.get(Instrument, body.record_ref)
                if inst is None:
                    return {"result": "already_absent"}
                s.delete(inst)
                return {"result": "deleted"}
        raise ValidationFailed("unknown record type")

    return app
