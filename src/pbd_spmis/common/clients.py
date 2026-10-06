"""Inter-service HTTP clients.

In ``allinone`` mode the client dispatches straight into the target service's ASGI app in
process, so the demo and the test-suite exercise the exact same request/response contracts as
a distributed deployment. In ``distributed`` mode the client talks HTTP to ``PBD_<SERVICE>_URL``.

Service identities
------------------
Every downstream call is made with a *service token* for the calling service (a distinct machine
identity per service, as the architecture requires), never by forwarding the end-user's token.
The originating actor and correlation id travel in headers for audit purposes.
"""

from __future__ import annotations

import atexit
import contextlib
import threading
from typing import Any

import anyio
import httpx
from anyio.from_thread import BlockingPortal, start_blocking_portal

from ..config import get_settings
from .auth import issue_token
from .errors import UpstreamUnavailable

_apps: dict[str, Any] = {}
_portal: BlockingPortal | None = None
_portal_cm = None
_lock = threading.Lock()


def register_app(service: str, app: Any) -> None:
    """Register an in-process ASGI app for ``service`` (used by the all-in-one composition)."""
    _apps[service] = app


def registered_apps() -> dict[str, Any]:
    return dict(_apps)


def clear_apps() -> None:
    _apps.clear()


def _get_portal() -> BlockingPortal:
    global _portal, _portal_cm
    with _lock:
        if _portal is None:
            _portal_cm = start_blocking_portal()
            _portal = _portal_cm.__enter__()
            atexit.register(close_portal)
    return _portal


def close_portal() -> None:
    """Stop the background event loop (registered with atexit; safe to call twice)."""
    global _portal, _portal_cm
    with _lock:
        if _portal_cm is not None:
            with contextlib.suppress(Exception):  # shutting down; nothing to recover
                _portal_cm.__exit__(None, None, None)
            _portal, _portal_cm = None, None


class InProcessTransport(httpx.BaseTransport):
    """Synchronous httpx transport that runs an ASGI app on a background event loop."""

    def __init__(self, app: Any):
        self._asgi = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        portal = _get_portal()

        async def _dispatch() -> httpx.Response:
            response = await self._asgi.handle_async_request(request)
            body = await response.aread()
            await response.aclose()
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=body,
                request=request,
            )

        try:
            return portal.call(_dispatch)
        except anyio.get_cancelled_exc_class():  # pragma: no cover
            raise


# Service name -> machine role as registered in catalog/policy.yaml.
SERVICE_ROLES = {
    "pdp": "PDP_SERVICE",
    "audit": "AUDIT_SERVICE",
    "vault": "VAULT_SERVICE",
    "registry": "REGISTRY_SERVICE",
    "program": "PROGRAM_SERVICE",
    "broker": "BROKER_SERVICE",
    "eligibility": "ELIGIBILITY_SERVICE",
    "breakglass": "BREAKGLASS_SERVICE",
    "payments": "PAYMENT_SERVICE",
    "retention": "RETENTION_SERVICE",
}


def service_token(service: str) -> str:
    settings = get_settings()
    return issue_token(
        settings.auth_signing_key,
        sub=f"svc:{service}",
        role=SERVICE_ROLES.get(service, f"{service.upper()}_SERVICE"),
        agency="SOCIAL_PROTECTION_AGENCY",
        programs=["*"],
        amr=["mtls"],
        svc=True,
        issuer=settings.auth_issuer,
        ttl_seconds=300,
    )


class ServiceClient:
    """A thin client for one downstream service."""

    def __init__(self, target: str, *, caller: str):
        self.target = target
        self.caller = caller
        settings = get_settings()
        url = settings.service_url(target)
        if settings.service_mode == "allinone" and url is None:
            app = _apps.get(target)
            if app is None:
                raise UpstreamUnavailable(f"service '{target}' is not registered in-process")
            self._client = httpx.Client(transport=InProcessTransport(app), base_url="http://" + target)
        else:
            self._client = httpx.Client(base_url=url or f"http://{target}:8000", timeout=10.0)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        hdrs = {"Authorization": f"Bearer {service_token(self.caller)}"}
        if headers:
            hdrs.update(headers)
        try:
            return self._client.request(method, path, headers=hdrs, json=json, params=params)
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"{self.target} unavailable: {exc.__class__.__name__}") from exc

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("POST", path, **kw)

    def close(self) -> None:
        self._client.close()


def client(target: str, *, caller: str) -> ServiceClient:
    return ServiceClient(target, caller=caller)
