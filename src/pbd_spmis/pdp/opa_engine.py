"""OPA-backed policy engine.

Delegates the decision to an Open Policy Agent instance that has loaded ``policy/rego`` and the
catalogue bundle. The decision document shape is identical to the embedded engine's, so the
Policy Decision Point can switch engines with one environment variable.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..common.errors import UpstreamUnavailable


class OpaEngine:
    def __init__(self, base_url: str, *, timeout: float = 2.0):
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def evaluate(self, inp: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post("/v1/data/spmis/authz/decision", json={"input": inp})
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"OPA unavailable: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"OPA returned {resp.status_code}")
        result = resp.json().get("result")
        if not isinstance(result, dict):
            raise UpstreamUnavailable("OPA returned no decision (policy not loaded?)")
        return result

    def healthy(self) -> bool:
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False
