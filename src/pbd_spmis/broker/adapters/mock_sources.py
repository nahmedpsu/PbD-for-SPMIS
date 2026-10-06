"""Mock authoritative sources for demos and tests.

The datasets are fictional. A real adapter replaces the in-memory dictionaries with a signed,
mutually authenticated call to the agency's verification API and keeps the same return shape.
"""

from __future__ import annotations

from typing import Any

# national_id -> monthly income (fictional)
_TAX: dict[str, float] = {
    "NID-1001": 180.0,
    "NID-1002": 900.0,
    "NID-1003": 120.0,
    "NID-2001": 300.0,
    "NID-2002": 150.0,
    "NID-3001": 95.0,
    "NID-3002": 210.0,
    "NID-3003": 240.0,
    "NID-3004": 60.0,
    "NID-3005": 130.0,
}

# national_id -> certified disability status
_DISABILITY: dict[str, str] = {
    "NID-2001": "certified_severe",
    "NID-2002": "certified_moderate",
    "NID-1002": "not_certified",
}

# national_id -> (verified, district, alive)
_CIVIL: dict[str, tuple[bool, str, bool]] = {
    "NID-1001": (True, "North", True),
    "NID-1002": (True, "Central", True),
    "NID-1003": (True, "North", False),
    "NID-2001": (True, "East", True),
    "NID-2002": (True, "North", True),
    "NID-3001": (True, "North", True),
    "NID-3002": (True, "North", True),
    "NID-3003": (True, "North", True),
    "NID-3004": (True, "South", True),
    "NID-3005": (True, "South", True),
}


class MockTaxAuthority:
    source = "tax-authority"

    def query(self, check: str, identifier: str, params: dict[str, Any]) -> dict[str, Any]:
        income = _TAX.get(identifier)
        if income is None:
            return {"status": "no_record"}
        if check == "income_threshold":
            threshold = float(params.get("threshold", 0))
            return {"status": "ok", "met": income < threshold, "raw_income_never_returned": income}
        if check == "income_band":
            bands = params.get("bands") or []
            label = next((b["label"] for b in bands if b["max"] is None or income < b["max"]), "unknown")
            return {"status": "ok", "band": label}
        return {"status": "unsupported_check"}


class MockDisabilityRegistry:
    source = "disability-registry"

    def query(self, check: str, identifier: str, params: dict[str, Any]) -> dict[str, Any]:
        status = _DISABILITY.get(identifier)
        if status is None:
            return {"status": "no_record"}
        if check == "disability_eligibility":
            eligible = status in set(params.get("eligible_statuses") or [])
            return {"status": "ok", "eligible": eligible, "certification_detail_never_returned": status}
        return {"status": "unsupported_check"}


class MockCivilRegistry:
    source = "civil-registry"

    def query(self, check: str, identifier: str, params: dict[str, Any]) -> dict[str, Any]:
        rec = _CIVIL.get(identifier)
        if rec is None:
            return {"status": "no_record"}
        verified, district, alive = rec
        if check == "identity_status":
            return {
                "status": "ok",
                "verified": verified and alive,
                "reference": f"CR-{abs(hash(identifier)) % 10**6:06d}",
            }
        if check == "residence":
            return {"status": "ok", "match": district == params.get("district")}
        return {"status": "unsupported_check"}
