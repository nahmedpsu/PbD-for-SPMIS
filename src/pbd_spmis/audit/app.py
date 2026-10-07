"""Audit / decision store service.

Properties
----------
* **Append-only and hash-chained.** Each event stores the hash of the previous event and its
  own hash over a canonical serialisation. ``GET /v1/chain/verify`` recomputes the chain and
  reports the first broken link, so tampering or deletion is detectable.
* **PII guard.** Events are rejected if they carry keys that belong in data stores rather than
  logs (national identifiers, names, contacts, account numbers) or free text long enough to be
  a narrative. The log records *that* something was released, never *what*.
* **Reading the log is itself logged.** Every query by a human produces an ``audit_read`` event.
* **Dashboards.** ``GET /v1/metrics`` serves the operational privacy dashboard counters from the
  guide (denied sensitive access, break-glass uses, exports, external sharing by purpose, ...).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import JSON, Integer, String, Text, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..common.context import RequestContext, service_context
from ..common.db import session_for
from ..common.errors import PolicyDenied, ValidationFailed, install_error_handlers
from ..common.ids import opaque

SERVICE = "audit"
GENESIS = "0" * 64
FORBIDDEN_KEYS = {
    "national_id",
    "nid",
    "name",
    "full_name",
    "contact",
    "phone",
    "email",
    "bank_account",
    "account_number",
    "iban",
    "date_of_birth",
    "dob",
    "narrative",
    "case_narrative",
    "street",
}
MAX_STRING = 256
_NID_PATTERN = re.compile(r"\bNID-\d{3,}\b")
# A forbidden key is tolerated only when its value is a release mode (e.g. {"national_id": "deny"}):
# that is the policy's answer about the attribute, not the attribute itself.
_RELEASE_MODE = re.compile(
    r"^(exact|band|token|verify_only|masked|deny|assertion:[a-z_]+|precision:[a-z_]+)$"
)
_write_lock = threading.Lock()


class Base(DeclarativeBase):
    pass


class AuditEvent(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    ts: Mapped[str] = mapped_column(String(40), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    service: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    actor_role: Mapped[str] = mapped_column(String(64))
    actor_agency: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str | None] = mapped_column(String(64), index=True)
    program: Mapped[str | None] = mapped_column(String(64), index=True)
    subject_token: Mapped[str | None] = mapped_column(String(64), index=True)
    attributes: Mapped[list[str]] = mapped_column(JSON, default=list)
    outcome: Mapped[str] = mapped_column(String(32), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(64), index=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    obligations: Mapped[list[str]] = mapped_column(JSON, default=list)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    canonical: Mapped[str] = mapped_column(Text)


class EventIn(BaseModel):
    event_type: str
    service: str
    actor_id: str
    actor_role: str = ""
    actor_agency: str = ""
    purpose: str | None = None
    program: str | None = None
    subject_token: str | None = None
    attributes: list[str] = Field(default_factory=list)
    outcome: str
    decision_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def pii_guard(details: Any, path: str = "details") -> None:
    """Reject payloads that carry direct identifiers or narrative text."""
    if isinstance(details, dict):
        for k, v in details.items():
            if str(k).lower() in FORBIDDEN_KEYS and not (isinstance(v, str) and _RELEASE_MODE.match(v)):
                raise ValidationFailed(f"audit payload must not contain '{path}.{k}'", code="pii_in_audit")
            pii_guard(v, f"{path}.{k}")
    elif isinstance(details, list):
        for i, v in enumerate(details):
            pii_guard(v, f"{path}[{i}]")
    elif isinstance(details, str):
        if len(details) > MAX_STRING:
            raise ValidationFailed(f"audit string at {path} exceeds {MAX_STRING} chars", code="pii_in_audit")
        if _NID_PATTERN.search(details):
            raise ValidationFailed(f"audit string at {path} looks like a national id", code="pii_in_audit")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def _event_dict(e: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": e.event_id,
        "seq": e.seq,
        "ts": e.ts,
        "event_type": e.event_type,
        "service": e.service,
        "actor_id": e.actor_id,
        "actor_role": e.actor_role,
        "actor_agency": e.actor_agency,
        "purpose": e.purpose,
        "program": e.program,
        "subject_token": e.subject_token,
        "attributes": e.attributes,
        "outcome": e.outcome,
        "decision_id": e.decision_id,
        "reason_codes": e.reason_codes,
        "obligations": e.obligations,
        "correlation_id": e.correlation_id,
        "details": e.details,
        "prev_hash": e.prev_hash,
        "hash": e.hash,
    }


def append_event(ev: EventIn) -> AuditEvent:
    pii_guard(ev.details)
    with _write_lock, session_for(SERVICE, Base) as s:
        last = s.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).scalar_one_or_none()
        prev_hash = last.hash if last else GENESIS
        body = ev.model_dump()
        body["event_id"] = opaque("AE", 12)
        body["ts"] = datetime.now(UTC).isoformat()
        canonical = canonical_json(body)
        e = AuditEvent(
            **body, prev_hash=prev_hash, hash=compute_hash(prev_hash, canonical), canonical=canonical
        )
        s.add(e)
        s.flush()
        return e


READ_ROLES = {"AUDITOR", "PRIVACY_ADMIN", "SUPERVISOR"}


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Audit Store", version="0.2.2")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/events", status_code=201)
    def write_event(ev: EventIn, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        e = append_event(ev)
        return {"event_id": e.event_id, "seq": e.seq, "hash": e.hash}

    def _require_reader(ctx: RequestContext) -> None:
        if ctx.actor.is_service or ctx.actor.role in READ_ROLES:
            return
        raise PolicyDenied("audit log access restricted to auditors", code="audit_read_denied")

    @app.get("/v1/events")
    def list_events(
        ctx: RequestContext = Depends(service_context),
        event_type: str | None = None,
        subject_token: str | None = None,
        actor_id: str | None = None,
        outcome: str | None = None,
        correlation_id: str | None = None,
        decision_id: str | None = None,
        limit: int = Query(default=100, le=1000),
    ) -> dict[str, Any]:
        _require_reader(ctx)
        with session_for(SERVICE, Base) as s:
            q = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
            if event_type:
                q = q.where(AuditEvent.event_type == event_type)
            if subject_token:
                q = q.where(AuditEvent.subject_token == subject_token)
            if actor_id:
                q = q.where(AuditEvent.actor_id == actor_id)
            if outcome:
                q = q.where(AuditEvent.outcome == outcome)
            if correlation_id:
                q = q.where(AuditEvent.correlation_id == correlation_id)
            if decision_id:
                q = q.where(AuditEvent.decision_id == decision_id)
            rows = [_event_dict(e) for e in s.execute(q).scalars()]
        if not ctx.actor.is_service:
            append_event(
                EventIn(
                    event_type="audit_read",
                    service=SERVICE,
                    actor_id=ctx.actor.id,
                    actor_role=ctx.actor.role,
                    actor_agency=ctx.actor.agency,
                    outcome="allow",
                    correlation_id=ctx.correlation_id,
                    details={"filters": {"event_type": event_type, "subject_token": subject_token}},
                )
            )
        return {"events": rows, "count": len(rows)}

    @app.get("/v1/chain/verify")
    def verify_chain(ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        _require_reader(ctx)
        with session_for(SERVICE, Base) as s:
            prev = GENESIS
            checked = 0
            for e in s.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars():
                expected = compute_hash(prev, e.canonical)
                if e.prev_hash != prev or e.hash != expected:
                    return {"ok": False, "checked": checked, "broken_at_seq": e.seq, "event_id": e.event_id}
                # The canonical text must still match the stored columns (detects in-place edits).
                stored = json.loads(e.canonical)
                live = {k: v for k, v in _event_dict(e).items() if k not in ("seq", "prev_hash", "hash")}
                if stored != live:
                    return {"ok": False, "checked": checked, "broken_at_seq": e.seq, "event_id": e.event_id}
                prev = e.hash
                checked += 1
        return {"ok": True, "checked": checked, "head": prev}

    @app.get("/v1/metrics")
    def metrics(ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        _require_reader(ctx)
        with session_for(SERVICE, Base) as s:

            def count(**where: Any) -> int:
                q = select(func.count()).select_from(AuditEvent)
                for k, v in where.items():
                    q = q.where(getattr(AuditEvent, k) == v)
                return int(s.execute(q).scalar_one())

            def grouped(col: str, **where: Any) -> dict[str, int]:
                column = getattr(AuditEvent, col)
                q = select(column, func.count()).select_from(AuditEvent)
                for k, v in where.items():
                    q = q.where(getattr(AuditEvent, k) == v)
                q = q.group_by(column)
                return {str(k): int(n) for k, n in s.execute(q).all()}

            return {
                "total_events": count(),
                "denied_access_by_program": grouped("program", event_type="access_decision", outcome="deny"),
                "denied_access_by_agency": grouped(
                    "actor_agency", event_type="access_decision", outcome="deny"
                ),
                "deny_reasons": _reason_histogram(s),
                "break_glass_requested": count(event_type="break_glass_requested"),
                "break_glass_activated": count(event_type="break_glass_activated"),
                "break_glass_reviewed": count(event_type="break_glass_reviewed"),
                "external_disclosures_by_purpose": grouped("purpose", event_type="external_disclosure"),
                "exports": count(event_type="export"),
                "token_resolutions": count(event_type="token_resolved"),
                "policy_errors": count(event_type="policy_error"),
                "retention_actions": grouped("outcome", event_type="retention_expired"),
                "audit_reads": count(event_type="audit_read"),
            }

    return app


def _reason_histogram(s: Any) -> dict[str, int]:
    hist: dict[str, int] = {}
    q = select(AuditEvent.reason_codes).where(AuditEvent.outcome == "deny")
    for (codes,) in s.execute(q).all():
        for c in codes or []:
            hist[c] = hist.get(c, 0) + 1
    return hist
