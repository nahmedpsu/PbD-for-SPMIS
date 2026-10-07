"""Optional hooks into openIMIS core signals.

openIMIS core exposes ``bind_service_signal`` so modules can observe service calls. When core is
importable we bind to the social protection and individual services to audit writes that do not
go through GraphQL (imports, workflows). When it is not (tests, other hosts), binding is a no-op.
"""

from __future__ import annotations

import contextlib
from typing import Any

from pbd_spmis.sdk import UnavailableError

from .config import current

WATCHED = (
    "individual_service.create",
    "individual_service.update",
    "individual_service.delete",
    "beneficiary_service.create",
    "beneficiary_service.update",
    "beneficiary_service.delete",
    "benefit_plan_service.create_beneficiaries",
)


def bind() -> None:
    try:
        from core.signals import bind_service_signal  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - openIMIS core not present
        return
    for name in WATCHED:
        with contextlib.suppress(Exception):  # signal not registered in this assembly
            bind_service_signal(name, _after_service_call, bind_type="after")


def _after_service_call(sender: Any, **kwargs: Any) -> None:
    cfg = current()
    result = kwargs.get("result") or {}
    user = kwargs.get("user")
    try:
        cfg.control_plane.audit.emit(
            "service_write",
            service="openimis",
            actor_id=str(getattr(user, "username", "") or getattr(user, "id", "") or "openimis"),
            outcome="success" if result.get("success", True) else "failure",
            details={
                "signal": str(sender),
                "entity": result.get("data", {}).get("__class__", "")
                if isinstance(result.get("data"), dict)
                else "",
            },
        )
    except UnavailableError:
        if cfg.fail_closed:
            raise
