"""Derive the PbD actor from an openIMIS user.

openIMIS authorises with numeric right codes attached to roles. The mapping file turns a user's
right codes into one PbD role; programs come from the request (benefit plan) because openIMIS
does not scope users to programs. Nothing personal about the user is sent to the control plane:
the actor id is the openIMIS username/id.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pbd_spmis.sdk.mapping import Mapping


def rights_of(user: Any) -> set[int]:
    """Collect right codes from the shapes openIMIS users come in."""
    rights: set[int] = set()
    for source in (
        getattr(user, "rights", None),
        getattr(getattr(user, "_u", None), "rights", None),
        getattr(user, "pbd_rights", None),
    ):
        if source is None:
            continue
        values = source() if callable(source) else source
        for v in _iter(values):
            try:
                rights.add(int(v))
            except (TypeError, ValueError):
                continue
    return rights


def _iter(values: Any) -> Iterable[Any]:
    if isinstance(values, dict):
        return values.keys()
    try:
        return list(values)
    except TypeError:
        return []


def actor_for(user: Any, mapping: Mapping, *, agency: str, programs: list[str]) -> dict[str, Any] | None:
    if user is None or not getattr(user, "is_authenticated", True):
        return None
    role = getattr(user, "pbd_role", None) or mapping.role_for_rights(rights_of(user))
    if role is None:
        return None
    is_service = bool(getattr(user, "is_technical", False) or getattr(user, "pbd_is_service", False))
    amr = ["pwd"]
    if getattr(user, "pbd_mfa", False) or getattr(user, "mfa_verified", False):
        amr.append("mfa")
    uid = getattr(user, "username", None) or getattr(user, "id", None) or getattr(user, "pk", None)
    return {
        "id": str(uid or ""),
        "role": role,
        "agency": agency,
        "office": str(getattr(user, "office", "") or ""),
        "programs": programs,
        "cases": [str(c) for c in (getattr(user, "pbd_cases", None) or [])],
        "amr": amr,
        "is_service": is_service,
    }
