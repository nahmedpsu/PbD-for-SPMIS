"""Payment provider adapter.

The adapter is the only code that sees a decrypted account. A real adapter signs a disbursement
request to the payment service provider / treasury system and returns the provider reference.
"""

from __future__ import annotations

from typing import Any

from ..common.ids import opaque


class MockPaymentProvider:
    name = "mock_bank"

    def disburse(
        self, *, account: str, beneficiary_name: str, amount: float, currency: str, reference: str
    ) -> dict[str, Any]:
        if account.endswith("0000"):
            return {"ok": False, "error": "ACCOUNT_CLOSED", "reference": None}
        if amount > 10_000:
            return {"ok": False, "error": "LIMIT_EXCEEDED", "reference": None}
        return {"ok": True, "reference": opaque("TXN", 10), "error": None}
