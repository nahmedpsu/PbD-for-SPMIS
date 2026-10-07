"""Release transformations, shared by the services, the SDK, the openIMIS module and the gateway.

They operate on a plain *catalogue view* dictionary as served by ``GET /pdp/v1/catalog``::

    {"version": "...", "attributes": {"income": {"class": "C4", "bands": [...]}, ...},
     "programs": {"cash_assistance": {"income_threshold": 250, "eligible_disability_statuses": [...]}}}

so that a client never needs the catalogue YAML files.
"""

from __future__ import annotations

import datetime
from typing import Any


def band_for(catalog: dict[str, Any], attr: str, value: Any) -> str | None:
    bands = (catalog.get("attributes", {}).get(attr) or {}).get("bands") or []
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    for band in bands:
        if band.get("max") is None or v < band["max"]:
            return band["label"]
    return bands[-1]["label"] if bands else None


def assert_for(
    catalog: dict[str, Any], attr: str, assertion: str, value: Any, *, program: str
) -> bool | None:
    prog = catalog.get("programs", {}).get(program) or {}
    if attr == "income" and assertion == "below_threshold":
        threshold = prog.get("income_threshold")
        if threshold is None or value is None:
            return None
        try:
            return float(value) < float(threshold)
        except (TypeError, ValueError):
            return None
    if attr == "disability_status" and assertion == "eligible":
        return value in set(prog.get("eligible_disability_statuses") or [])
    return None


def precision_for(value: Any, level: str) -> Any:
    if isinstance(value, list):
        return [precision_for(v, level) for v in value]
    if isinstance(value, datetime.date):  # datetime is a date subclass
        if level == "year":
            return datetime.date(value.year, 1, 1)
        if level == "month":
            return datetime.date(value.year, value.month, 1)
        return value
    if isinstance(value, dict):
        return {level: value.get(level)}
    if isinstance(value, str) and level == "year":
        return value[:4]
    if isinstance(value, str) and level == "month":
        return value[:7]
    return value


def masked(value: Any) -> str:
    s = str(value)
    return s[-2:].rjust(len(s), "*") if len(s) > 2 else "**"


def transform(attr: str, value: Any, mode: str, *, program: str, catalog: dict[str, Any]) -> Any:
    """Apply one release mode to one raw value."""
    if value is None:
        return None
    if mode == "exact":
        return value
    if mode == "band":
        return band_for(catalog, attr, value)
    if mode.startswith("assertion:"):
        return assert_for(catalog, attr, mode.split(":", 1)[1], value, program=program)
    if mode.startswith("precision:"):
        return precision_for(value, mode.split(":", 1)[1])
    if mode == "token":
        return {"token": value.get("token") if isinstance(value, dict) else value}
    if mode == "verify_only":
        return {"verified": bool(value)}
    if mode == "masked":
        return masked(value)
    if mode == "deny":
        return None
    raise ValueError(f"unknown release mode '{mode}'")


def apply_release(
    record: dict[str, Any], release: dict[str, str], *, program: str, catalog: dict[str, Any]
) -> dict[str, Any]:
    """Return only the attributes the release map permits, each transformed to its mode."""
    out: dict[str, Any] = {}
    for attr, mode in release.items():
        if mode == "deny" or attr not in record:
            continue
        out[attr] = transform(attr, record[attr], mode, program=program, catalog=catalog)
    return out
