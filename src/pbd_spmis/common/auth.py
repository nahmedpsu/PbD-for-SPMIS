"""Authentication: compact JWT (HS256) issue/verify and the actor model.

The reference implementation ships its own tiny HS256 signer so that the demo runs with zero
external infrastructure. The verification seam (``verify_token``) is where an OIDC provider such
as Keycloak or a government identity provider plugs in: swap the signature check for JWKS
validation and map the provider's claims to :class:`Actor`.

Claims
------
sub       stable actor identifier (user id or service identity)
role      coarse role, e.g. CASE_WORKER, REGISTRATION_OFFICER, PAYMENT_SERVICE
agency    organisation the actor belongs to
office    optional office/district
programs  list of program codes the actor is assigned to
cases     list of case ids the actor is assigned to
amr       authentication methods, e.g. ["pwd", "mfa"]
svc       true for machine identities
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import AuthenticationError


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    agency: str
    office: str = ""
    programs: tuple[str, ...] = ()
    cases: tuple[str, ...] = ()
    amr: tuple[str, ...] = ("pwd",)
    is_service: bool = False
    claims: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def has_mfa(self) -> bool:
        return "mfa" in self.amr

    def to_policy_input(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "agency": self.agency,
            "office": self.office,
            "programs": list(self.programs),
            "cases": list(self.cases),
            "amr": list(self.amr),
            "is_service": self.is_service,
        }


def issue_token(
    signing_key: str,
    *,
    sub: str,
    role: str,
    agency: str,
    office: str = "",
    programs: list[str] | None = None,
    cases: list[str] | None = None,
    amr: list[str] | None = None,
    svc: bool = False,
    issuer: str = "pbd-spmis-dev",
    ttl_seconds: int = 3600,
) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "iss": issuer,
        "sub": sub,
        "role": role,
        "agency": agency,
        "office": office,
        "programs": programs or [],
        "cases": cases or [],
        "amr": amr or ["pwd"],
        "svc": svc,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(payload).encode())}"
    sig = hmac.new(signing_key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def verify_token(signing_key: str, token: str, *, issuer: str | None = None) -> Actor:
    try:
        h, p, s = token.split(".")
    except ValueError as exc:
        raise AuthenticationError("malformed token") from exc
    header = json.loads(_unb64url(h))
    if header.get("alg") != "HS256":
        raise AuthenticationError("unsupported algorithm")
    expected = hmac.new(signing_key.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64url(s)):
        raise AuthenticationError("invalid signature")
    payload = json.loads(_unb64url(p))
    if payload.get("exp", 0) < time.time():
        raise AuthenticationError("token expired")
    if issuer and payload.get("iss") != issuer:
        raise AuthenticationError("unexpected issuer")
    return actor_from_claims(payload)


def actor_from_claims(claims: dict[str, Any]) -> Actor:
    return Actor(
        id=str(claims["sub"]),
        role=str(claims.get("role", "")),
        agency=str(claims.get("agency", "")),
        office=str(claims.get("office", "")),
        programs=tuple(claims.get("programs", [])),
        cases=tuple(claims.get("cases", [])),
        amr=tuple(claims.get("amr", ["pwd"])),
        is_service=bool(claims.get("svc", False)),
        claims=claims,
    )
