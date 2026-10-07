"""Retention and deletion engine.

Services register records when they create them; the engine computes the expiry from the
catalogue (period and end action per record type) and, when due, calls the owning service's
``/v1/internal/retention`` endpoint under the ``retention_processing`` purpose. Every executed
action produces a ``retention_expired`` audit event, and failures stay visible on the dashboard.

POST /v1/schedules            register a record (idempotent per service/record_type/record_ref)
GET  /v1/schedules            list; ?due_within_days=N for the "approaching expiry" dashboard
POST /v1/run                  execute due actions (dry_run supported, as_of for testing)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel
from sqlalchemy import String, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..catalog.loader import get_catalog
from ..common.audit_client import emit
from ..common.clients import client
from ..common.context import RequestContext, service_context
from ..common.db import session_for
from ..common.errors import PolicyDenied, UpstreamUnavailable, install_error_handlers
from ..common.ids import opaque

SERVICE = "retention"
RUN_ROLES = {"PRIVACY_ADMIN", "RETENTION_SERVICE"}


class Base(DeclarativeBase):
    pass


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (UniqueConstraint("service", "record_type", "record_ref", name="uq_schedule"),)

    schedule_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    service: Mapped[str] = mapped_column(String(32))
    record_type: Mapped[str] = mapped_column(String(64), index=True)
    record_ref: Mapped[str] = mapped_column(String(64), index=True)
    program: Mapped[str] = mapped_column(String(64))
    data_class: Mapped[str] = mapped_column(String(4))
    end_action: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="scheduled")
    executed_at: Mapped[str | None] = mapped_column(String(40), default=None)
    result: Mapped[str | None] = mapped_column(String(64), default=None)


class ScheduleIn(BaseModel):
    service: str
    record_type: str
    record_ref: str
    program: str
    created_at: str | None = None


class RunIn(BaseModel):
    dry_run: bool = True
    as_of: str | None = None
    limit: int = 500


def _now() -> datetime:
    return datetime.now(UTC)


def _to_dict(s: Schedule) -> dict[str, Any]:
    return {
        k: getattr(s, k)
        for k in (
            "schedule_id",
            "service",
            "record_type",
            "record_ref",
            "program",
            "data_class",
            "end_action",
            "created_at",
            "expires_at",
            "status",
            "executed_at",
            "result",
        )
    }


def compute_expiry(record_type: str, created_at: datetime) -> tuple[datetime, str, str]:
    rule = get_catalog().retention_rule(record_type)
    if "period_hours" in rule:
        delta = timedelta(hours=int(rule["period_hours"]))
    else:
        delta = timedelta(days=int(rule.get("period_days", 365)))
    return created_at + delta, rule["end_action"], rule.get("class", "C3")


def create_app() -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Retention Engine", version="0.2.0")
    install_error_handlers(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/schedules", status_code=201)
    def register(body: ScheduleIn, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        if not ctx.actor.is_service and ctx.actor.role not in RUN_ROLES:
            raise PolicyDenied("only services register retention schedules", code="service_only")
        created = datetime.fromisoformat(body.created_at) if body.created_at else _now()
        expires, action, data_class = compute_expiry(body.record_type, created)
        with session_for(SERVICE, Base) as s:
            existing = s.execute(
                select(Schedule).where(
                    Schedule.service == body.service,
                    Schedule.record_type == body.record_type,
                    Schedule.record_ref == body.record_ref,
                )
            ).scalar_one_or_none()
            if existing:
                return _to_dict(existing)
            row = Schedule(
                schedule_id=opaque("RS", 10),
                service=body.service,
                record_type=body.record_type,
                record_ref=body.record_ref,
                program=body.program,
                data_class=data_class,
                end_action=action,
                created_at=created.isoformat(),
                expires_at=expires.isoformat(),
            )
            s.add(row)
            s.flush()
            return _to_dict(row)

    @app.get("/v1/schedules")
    def list_schedules(
        ctx: RequestContext = Depends(service_context),
        due_within_days: int | None = Query(default=None, ge=0),
        status: str | None = None,
    ) -> dict[str, Any]:
        if not ctx.actor.is_service and ctx.actor.role not in RUN_ROLES | {"AUDITOR"}:
            raise PolicyDenied("not permitted", code="ROLE_NOT_PERMITTED")
        with session_for(SERVICE, Base) as s:
            q = select(Schedule).order_by(Schedule.expires_at.asc())
            if status:
                q = q.where(Schedule.status == status)
            if due_within_days is not None:
                q = q.where(Schedule.expires_at <= (_now() + timedelta(days=due_within_days)).isoformat())
            rows = [_to_dict(r) for r in s.execute(q).scalars()]
        return {"schedules": rows, "count": len(rows)}

    @app.post("/v1/run")
    def run(body: RunIn, ctx: RequestContext = Depends(service_context)) -> dict[str, Any]:
        if ctx.actor.role not in RUN_ROLES:
            raise PolicyDenied(
                "only the retention service or a privacy administrator may run retention",
                code="ROLE_NOT_PERMITTED",
            )
        as_of = datetime.fromisoformat(body.as_of) if body.as_of else _now()
        with session_for(SERVICE, Base) as s:
            due = [
                _to_dict(r)
                for r in s.execute(
                    select(Schedule)
                    .where(Schedule.status == "scheduled", Schedule.expires_at <= as_of.isoformat())
                    .order_by(Schedule.expires_at.asc())
                    .limit(body.limit)
                ).scalars()
            ]
        summary: dict[str, int] = {"due": len(due), "executed": 0, "failed": 0, "dry_run": int(body.dry_run)}
        outcomes: list[dict[str, Any]] = []
        for item in due:
            if body.dry_run:
                outcomes.append({**item, "would": item["end_action"]})
                continue
            result, ok = _execute(ctx, item)
            with session_for(SERVICE, Base) as s:
                row = s.get(Schedule, item["schedule_id"])
                assert row is not None
                row.status = "executed" if ok else "failed"
                row.executed_at, row.result = _now().isoformat(), result
            summary["executed" if ok else "failed"] += 1
            emit(
                SERVICE,
                ctx,
                "retention_expired",
                outcome=item["end_action"] if ok else "failed",
                subject_token=item["record_ref"] if item["record_ref"].startswith("P-") else None,
                program=item["program"],
                purpose="retention_processing",
                details={
                    "record_type": item["record_type"],
                    "record_ref": item["record_ref"],
                    "result": result,
                },
            )
            outcomes.append({**item, "result": result})
        return {"summary": summary, "as_of": as_of.isoformat(), "items": outcomes}

    return app


def _execute(ctx: RequestContext, item: dict[str, Any]) -> tuple[str, bool]:
    headers = {
        "X-Purpose": "retention_processing",
        "X-Program": item["program"],
        "X-Correlation-ID": ctx.correlation_id,
    }
    try:
        resp = client(item["service"], caller=SERVICE).post(
            "/v1/internal/retention",
            headers=headers,
            json={
                "record_type": item["record_type"],
                "record_ref": item["record_ref"],
                "action": item["end_action"],
            },
        )
    except UpstreamUnavailable as exc:
        return f"error:{exc.code}", False
    if resp.status_code != 200:
        return f"error:http_{resp.status_code}", False
    return str(resp.json().get("result", "ok")), True
