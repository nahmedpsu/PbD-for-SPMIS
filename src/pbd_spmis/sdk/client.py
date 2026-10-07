"""HTTP client for the Privacy Control Plane API v1.

The client is synchronous and thread-safe for independent requests. It raises
:class:`PolicyDeniedError` on 403 with the reason codes, :class:`UnavailableError` on transport
errors and 5xx (so callers fail closed), and :class:`PrivacyError` for other non-2xx responses.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from .models import Decision, PolicyDeniedError, PrivacyError, UnavailableError
from .transforms import apply_release as _apply_release

TokenProvider = Callable[[], str]

SERVICE_PATHS = {
    "pdp": "/pdp",
    "audit": "/audit",
    "vault": "/vault",
    "broker": "/broker",
    "breakglass": "/breakglass",
    "registry": "/registry",
    "program": "/program",
    "eligibility": "/eligibility",
    "payments": "/payments",
    "retention": "/retention",
}


class _Base:
    def __init__(self, cp: PrivacyControlPlane, service: str):
        self._cp = cp
        self._service = service

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._cp._request(self._service, method, path, headers=headers, json=json, params=params)


def privacy_headers(
    *,
    purpose: str | None = None,
    program: str | None = None,
    correlation_id: str | None = None,
    channel: str | None = None,
    device_trust: str | None = None,
    case_id: str | None = None,
    break_glass_grant: str | None = None,
) -> dict[str, str]:
    h: dict[str, str] = {}
    if purpose:
        h["X-Purpose"] = purpose
    if program:
        h["X-Program"] = program
    if correlation_id:
        h["X-Correlation-ID"] = correlation_id
    if channel:
        h["X-Channel"] = channel
    if device_trust:
        h["X-Device-Trust"] = device_trust
    if case_id:
        h["X-Case-ID"] = case_id
    if break_glass_grant:
        h["X-Break-Glass-Grant"] = break_glass_grant
    return h


class PdpClient(_Base):
    def decide(
        self,
        *,
        actor: dict[str, Any],
        program: str,
        purpose: str,
        attributes: list[str],
        subject_token: str | None = None,
        subject_programs: list[str] | None = None,
        action: str = "read",
        context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Decision:
        payload = {
            "actor": actor,
            "subject": {"person_token": subject_token, "program_relationship": subject_programs or []},
            "program": program,
            "purpose": purpose,
            "action": action,
            "attributes": attributes,
            "context": context or {},
        }
        data = self._request(
            "POST", "/v1/decisions", json=payload, headers=privacy_headers(correlation_id=correlation_id)
        )
        return Decision.from_json(data)

    def enforce(self, **kw: Any) -> Decision:
        """``decide`` that raises :class:`PolicyDeniedError` when the decision is deny."""
        d = self.decide(**kw)
        if not d.allow:
            raise PolicyDeniedError(
                "access denied by policy",
                code="policy_denied",
                status=403,
                body={"decision_id": d.decision_id, "reason_codes": d.reason_codes},
            )
        return d

    def catalog(self) -> dict[str, Any]:
        return self._request("GET", "/v1/catalog")

    def policy(self) -> dict[str, Any]:
        return self._request("GET", "/v1/policy")


class AuditClient(_Base):
    def emit(
        self,
        event_type: str,
        *,
        actor_id: str,
        outcome: str,
        service: str = "sdk",
        actor_role: str = "",
        actor_agency: str = "",
        purpose: str | None = None,
        program: str | None = None,
        subject_token: str | None = None,
        attributes: list[str] | None = None,
        decision_id: str | None = None,
        reason_codes: list[str] | None = None,
        obligations: list[str] | None = None,
        correlation_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        body = {
            "event_type": event_type,
            "service": service,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "actor_agency": actor_agency,
            "purpose": purpose,
            "program": program,
            "subject_token": subject_token,
            "attributes": attributes or [],
            "outcome": outcome,
            "decision_id": decision_id,
            "reason_codes": reason_codes or [],
            "obligations": obligations or [],
            "correlation_id": correlation_id,
            "details": details or {},
        }
        return str(self._request("POST", "/v1/events", json=body)["event_id"])

    def verify_chain(self) -> dict[str, Any]:
        return self._request("GET", "/v1/chain/verify")

    def metrics(self) -> dict[str, Any]:
        return self._request("GET", "/v1/metrics")

    def events(self, **filters: Any) -> list[dict[str, Any]]:
        return list(self._request("GET", "/v1/events", params=filters)["events"])


class VaultClient(_Base):
    def tokenize(
        self,
        *,
        national_id: str,
        name: str,
        contact: str = "",
        program: str,
        proofing_reference: str = "",
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Identity proofing: create or find the person token for an identity."""
        return self._request(
            "POST",
            "/v1/identities",
            json={
                "national_id": national_id,
                "name": name,
                "contact": contact,
                "proofing_reference": proofing_reference,
            },
            headers=privacy_headers(
                purpose="identity_proofing", program=program, correlation_id=correlation_id
            ),
        )

    def program_identifier(
        self, *, person_token: str, program: str, correlation_id: str | None = None
    ) -> str:
        data = self._request(
            "POST",
            "/v1/program-identifiers",
            json={"person_token": person_token, "program": program},
            headers=privacy_headers(purpose="enrollment", program=program, correlation_id=correlation_id),
        )
        return str(data["program_person_id"])

    def resolve(
        self,
        *,
        person_token: str,
        attributes: list[str],
        purpose: str,
        program: str,
        break_glass_grant: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/v1/resolve",
            json={"person_token": person_token, "attributes": attributes},
            headers=privacy_headers(
                purpose=purpose,
                program=program,
                break_glass_grant=break_glass_grant,
                correlation_id=correlation_id,
            ),
        )
        return dict(data["attributes"])

    def identity_status(self, *, person_token: str, program: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v1/identities/{person_token}/status",
            headers=privacy_headers(purpose="identity_proofing", program=program),
        )


class BrokerClient(_Base):
    def verify(
        self,
        *,
        person_token: str,
        checks: list[str],
        purpose: str,
        program: str,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/verify",
            json={"person_token": person_token, "checks": checks, "params": params or {}},
            headers=privacy_headers(purpose=purpose, program=program, correlation_id=correlation_id),
        )


class BreakGlassClient(_Base):
    def request(
        self,
        *,
        subject_token: str,
        program: str,
        attributes: list[str],
        reason_code: str,
        reason: str,
        duration_minutes: int = 60,
        case_ref: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/grants",
            json={
                "subject_token": subject_token,
                "program": program,
                "attributes": attributes,
                "reason_code": reason_code,
                "reason": reason,
                "duration_minutes": duration_minutes,
                "case_ref": case_ref,
            },
        )

    def approve(self, grant_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/grants/{grant_id}/approve")

    def revoke(self, grant_id: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/grants/{grant_id}/revoke")

    def get(self, grant_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/grants/{grant_id}")


class PrivacyControlPlane:
    """Entry point: one base URL (all-in-one) or per-service URLs (distributed)."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: TokenProvider | str | None = None,
        service_urls: dict[str, str] | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        catalog_ttl_seconds: float = 300.0,
    ):
        self._base = (base_url or "").rstrip("/")
        self._service_urls = {k: v.rstrip("/") for k, v in (service_urls or {}).items()}
        self._token = token
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._catalog: dict[str, Any] | None = None
        self._catalog_at = 0.0
        self._catalog_ttl = catalog_ttl_seconds
        self._lock = threading.Lock()
        self.pdp = PdpClient(self, "pdp")
        self.audit = AuditClient(self, "audit")
        self.vault = VaultClient(self, "vault")
        self.broker = BrokerClient(self, "broker")
        self.breakglass = BreakGlassClient(self, "breakglass")

    # ------------------------------------------------------------------ plumbing
    def _url(self, service: str, path: str) -> str:
        if service in self._service_urls:
            return self._service_urls[service] + path
        if not self._base:
            raise UnavailableError(f"no URL configured for service '{service}'", code="not_configured")
        return self._base + SERVICE_PATHS[service] + path

    def _auth_header(self) -> dict[str, str]:
        if self._token is None:
            return {}
        tok = self._token() if callable(self._token) else self._token
        return {"Authorization": f"Bearer {tok}"}

    def _request(
        self,
        service: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hdrs = self._auth_header()
        if headers:
            hdrs.update(headers)
        try:
            resp = self._client.request(
                method, self._url(service, path), headers=hdrs, json=json, params=params
            )
        except httpx.HTTPError as exc:
            raise UnavailableError(
                f"{service} unreachable: {exc.__class__.__name__}", code="unreachable"
            ) from exc
        if resp.status_code >= 500:
            raise UnavailableError(
                f"{service} returned {resp.status_code}",
                code="upstream_error",
                status=resp.status_code,
                body=_safe_json(resp),
            )
        if resp.status_code == 403:
            body = _safe_json(resp)
            raise PolicyDeniedError(
                body.get("detail", "denied"), code=body.get("error", "policy_denied"), status=403, body=body
            )
        if resp.status_code >= 400:
            body = _safe_json(resp)
            raise PrivacyError(
                str(body.get("detail", resp.text[:200])),
                code=str(body.get("error", "error")),
                status=resp.status_code,
                body=body,
            )
        return _safe_json(resp)

    # ------------------------------------------------------------------ helpers
    def catalog(self) -> dict[str, Any]:
        """The non-sensitive catalogue view (cached) used by the release transformations."""
        with self._lock:
            if self._catalog is None or time.monotonic() - self._catalog_at > self._catalog_ttl:
                self._catalog = self.pdp.catalog()
                self._catalog_at = time.monotonic()
            return self._catalog

    def apply_release(self, record: dict[str, Any], decision: Decision, *, program: str) -> dict[str, Any]:
        return _apply_release(record, decision.release, program=program, catalog=self.catalog())

    def close(self) -> None:
        self._client.close()


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"data": data}
