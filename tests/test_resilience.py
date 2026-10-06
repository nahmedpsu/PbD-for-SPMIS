"""Fail-closed behaviour and service-boundary properties."""

from __future__ import annotations

import os

import pytest

from pbd_spmis.common.clients import clear_apps, registered_apps
from pbd_spmis.config import reset_settings


@pytest.fixture()
def amina(harness):
    return harness.register_person(national_id="NID-1001", name="Amina Example", income=180, household_size=4)


def test_requests_without_purpose_or_token_are_rejected(harness, amina):
    tok = amina["person_token"]
    no_auth = harness.client("registry").get(
        f"/v1/persons/{tok}", headers={"X-Purpose": "registration", "X-Program": "cash_assistance"}
    )
    assert no_auth.status_code == 401
    h = harness.headers(
        sub="cw-1", role="CASE_WORKER", program="cash_assistance", programs=["cash_assistance"]
    )
    no_purpose = harness.client("registry").get(f"/v1/persons/{tok}", headers=h)
    assert no_purpose.status_code == 422 and no_purpose.json()["error"] == "purpose_required"


def test_sensitive_reads_fail_closed_when_pdp_is_unavailable(harness, amina):
    tok = amina["person_token"]
    cw = harness.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose="eligibility_verification",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    apps = registered_apps()
    clear_apps()
    try:
        for name, app in apps.items():
            if name != "pdp":
                from pbd_spmis.common.clients import register_app

                register_app(name, app)
        r = harness.client("registry").get(
            f"/v1/persons/{tok}", headers=cw, params={"attributes": "household_size"}
        )
        assert r.status_code == 503 and r.json()["error"] == "upstream_unavailable"
    finally:
        clear_apps()
        for name, app in apps.items():
            from pbd_spmis.common.clients import register_app

            register_app(name, app)


def test_disclosure_fails_closed_when_audit_is_unavailable(harness, amina):
    tok = amina["person_token"]
    cw = harness.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose="eligibility_verification",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    apps = registered_apps()
    from pbd_spmis.common.clients import register_app

    clear_apps()
    try:
        for name, app in apps.items():
            if name != "audit":
                register_app(name, app)
        r = harness.client("eligibility").post(
            "/v1/eligibility/verify", headers=cw, json={"person_token": tok}
        )
        assert r.status_code == 503
    finally:
        clear_apps()
        for name, app in apps.items():
            register_app(name, app)


def test_distributed_mode_uses_service_urls(monkeypatch):
    monkeypatch.setenv("PBD_SERVICE_MODE", "distributed")
    monkeypatch.setenv("PBD_PDP_URL", "http://pdp.internal:8000")
    reset_settings()
    from pbd_spmis.common.clients import ServiceClient

    c = ServiceClient("pdp", caller="registry")
    assert str(c._client.base_url).startswith("http://pdp.internal:8000")
    c.close()
    monkeypatch.delenv("PBD_PDP_URL")
    monkeypatch.setenv("PBD_SERVICE_MODE", "allinone")
    reset_settings()
    os.environ.pop("PBD_PDP_URL", None)


def test_root_index_lists_services(harness):
    import httpx

    from pbd_spmis.common.clients import InProcessTransport

    with httpx.Client(transport=InProcessTransport(harness.app), base_url="http://root") as c:
        body = c.get("/").json()
        assert set(body["services"]) == {
            "pdp",
            "audit",
            "vault",
            "registry",
            "program",
            "broker",
            "eligibility",
            "breakglass",
            "payments",
            "retention",
        }
        assert c.get("/pdp/healthz").json()["status"] == "ok"
        assert c.get("/vault/openapi.json").status_code == 200
