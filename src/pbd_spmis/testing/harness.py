"""In-process harness that boots the whole platform against fresh SQLite databases.

Used by the test-suite and by ``pbd-spmis demo``. It issues tokens for any role, builds the
headers a request needs, and exposes a client for every service.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import httpx

from ..catalog.loader import reset_catalog
from ..common.auth import issue_token
from ..common.clients import InProcessTransport, clear_apps
from ..common.db import reset_engines
from ..config import SERVICES, get_settings, reset_settings


class Harness:
    def __init__(
        self, *, policy_engine: str = "embedded", opa_url: str | None = None, tmp_dir: str | None = None
    ):
        self.tmp_dir = tmp_dir or tempfile.mkdtemp(prefix="pbd-spmis-")
        os.environ["PBD_SERVICE_MODE"] = "allinone"
        os.environ["PBD_DATABASE_URL"] = f"sqlite:///{self.tmp_dir}/{{service}}.sqlite3"
        os.environ["PBD_POLICY_ENGINE"] = policy_engine
        if opa_url:
            os.environ["PBD_OPA_URL"] = opa_url
        os.environ.setdefault("PBD_AUTH_SIGNING_KEY", "harness-signing-key")
        os.environ.setdefault("PBD_VAULT_MASTER_KEY", "")
        os.environ.setdefault("PBD_INDEX_HMAC_KEY", "harness-index-key")
        for s in SERVICES:
            os.environ.pop(f"PBD_{s.upper()}_URL", None)
        reset_settings()
        reset_catalog()
        reset_engines()
        clear_apps()
        from ..app import create_app

        self.app = create_app()
        self.settings = get_settings()
        self._clients: dict[str, httpx.Client] = {}

    # ------------------------------------------------------------------ clients
    def client(self, service: str) -> httpx.Client:
        from ..common.clients import registered_apps

        if service not in self._clients:
            self._clients[service] = httpx.Client(
                transport=InProcessTransport(registered_apps()[service]), base_url=f"http://{service}"
            )
        return self._clients[service]

    # ------------------------------------------------------------------ identities
    def token(
        self,
        *,
        sub: str,
        role: str,
        agency: str = "SOCIAL_PROTECTION_AGENCY",
        office: str = "",
        programs: list[str] | None = None,
        cases: list[str] | None = None,
        mfa: bool = False,
        svc: bool = False,
    ) -> str:
        return issue_token(
            self.settings.auth_signing_key,
            sub=sub,
            role=role,
            agency=agency,
            office=office,
            programs=programs,
            cases=cases,
            amr=["pwd", "mfa"] if mfa else ["pwd"],
            svc=svc,
            issuer=self.settings.auth_issuer,
        )

    def headers(
        self,
        *,
        sub: str,
        role: str,
        purpose: str | None = None,
        program: str | None = None,
        programs: list[str] | None = None,
        cases: list[str] | None = None,
        mfa: bool = False,
        agency: str = "SOCIAL_PROTECTION_AGENCY",
        device_trust: str = "managed",
        channel: str = "api",
        case_id: str | None = None,
        break_glass_grant: str | None = None,
        svc: bool = False,
        correlation_id: str | None = None,
    ) -> dict[str, str]:
        h = {
            "Authorization": "Bearer "
            + self.token(sub=sub, role=role, agency=agency, programs=programs, cases=cases, mfa=mfa, svc=svc),
            "X-Device-Trust": device_trust,
            "X-Channel": channel,
        }
        if purpose:
            h["X-Purpose"] = purpose
        if program:
            h["X-Program"] = program
        if case_id:
            h["X-Case-ID"] = case_id
        if break_glass_grant:
            h["X-Break-Glass-Grant"] = break_glass_grant
        if correlation_id:
            h["X-Correlation-ID"] = correlation_id
        return h

    # ------------------------------------------------------------------ convenience flows
    def register_person(
        self,
        *,
        national_id: str,
        name: str,
        contact: str = "",
        program: str = "cash_assistance",
        income: float | None = None,
        household_size: int | None = None,
        disability_status: str | None = None,
        employment_status: str | None = None,
        vulnerability: list[str] | None = None,
        district: str = "North",
        region: str = "Northern",
        street: str = "1 Example Street",
        officer: str = "officer-1",
    ) -> dict[str, Any]:
        h = self.headers(
            sub=officer,
            role="REGISTRATION_OFFICER",
            purpose="identity_proofing",
            program=program,
            programs=[program],
        )
        ident = self.client("vault").post(
            "/v1/identities",
            headers=h,
            json={
                "national_id": national_id,
                "name": name,
                "contact": contact,
                "proofing_reference": "KIOSK-7",
            },
        )
        ident.raise_for_status()
        token = ident.json()["person_token"]
        h["X-Purpose"] = "registration"
        reg = self.client("registry").post(
            "/v1/persons",
            headers=h,
            json={
                "person_token": token,
                "income": income,
                "household_size": household_size,
                "disability_status": disability_status,
                "employment_status": employment_status,
                "vulnerability_attributes": vulnerability or [],
                "address": {"street": street, "district": district, "region": region},
            },
        )
        reg.raise_for_status()
        return {"person_token": token, "household_token": reg.json()["household_token"]}

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
