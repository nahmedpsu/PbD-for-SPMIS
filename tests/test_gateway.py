"""Gateway tests: a fake upstream (openIMIS-like GraphQL + FHIR Patient) behind the privacy gateway,
with the real in-process control plane."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request

from pbd_spmis.common.clients import InProcessTransport
from pbd_spmis.gateway import GatewayConfig, create_gateway
from pbd_spmis.sdk import PrivacyControlPlane
from pbd_spmis.sdk.mapping import Mapping

ROOT = Path(__file__).resolve().parents[1]

UPSTREAM_INDIVIDUALS = {
    "data": {
        "individual": {
            "edges": [
                {
                    "node": {
                        "uuid": "1111",
                        "firstName": "Amina",
                        "lastName": "Example",
                        "dob": "1990-05-04",
                        "jsonExt": json.dumps(
                            {
                                "national_id": "NID-1001",
                                "income": 180,
                                "household_size": 4,
                                "address": {"district": "North", "street": "12 Market Lane"},
                            }
                        ),
                    }
                },
                {
                    "node": {
                        "uuid": "2222",
                        "firstName": "Bo",
                        "lastName": "Example",
                        "dob": "1975-01-30",
                        "jsonExt": json.dumps({"national_id": "NID-1002", "income": 900}),
                    }
                },
            ]
        }
    }
}

PATIENT = {
    "resourceType": "Patient",
    "id": "p-1",
    "name": [{"family": "Example", "given": ["Amina"]}],
    "birthDate": "1990-05-04",
    "identifier": [{"system": "nid", "value": "NID-1001"}],
    "telecom": [{"value": "+1"}],
    "address": [{"district": "North", "line": ["12 Market Lane"]}],
}


def fake_upstream() -> FastAPI:
    app = FastAPI()

    @app.post("/api/graphql")
    async def graphql(request: Request):
        body = await request.json()
        if "individual" in body.get("query", ""):
            return UPSTREAM_INDIVIDUALS
        if "unmapped" in body.get("query", ""):
            return {"data": {"unmapped": "visible", "secret": "still-there"}}
        return {"data": {}}

    @app.get("/api/api_fhir_r4/Patient/{pid}")
    def patient(pid: str):
        return PATIENT

    @app.get("/api/other")
    def other():
        return {"passthrough": True}

    return app


@pytest.fixture()
def gateway(harness):
    cp = PrivacyControlPlane(
        "http://cp",
        token=lambda: harness.token(sub="svc:gateway", role="REGISTRY_SERVICE", programs=["*"], svc=True),
        transport=InProcessTransport(harness.app),
    )
    cfg = GatewayConfig(
        upstream="http://upstream",
        mapping=Mapping.load(ROOT / "integrations/openimis/mapping.yaml"),
        control_plane=cp,
        actor_source="header",
    )
    app = create_gateway(cfg, upstream_transport=InProcessTransport(fake_upstream()))
    client = httpx.Client(transport=InProcessTransport(app), base_url="http://gw")
    yield client
    client.close()
    cp.close()


QUERY = {"query": "query { individual { edges { node { uuid firstName lastName dob jsonExt } } } }"}
CW = {"X-Actor-Id": "cw-1", "X-Actor-Rights": "170001"}


def test_graphql_response_is_minimised(gateway):
    r = gateway.post("/api/graphql", json=QUERY, headers={**CW, "X-Purpose": "eligibility_verification"})
    assert r.status_code == 200, r.text
    assert "no_export" in r.headers["x-obligations"]
    nodes = [e["node"] for e in r.json()["data"]["individual"]["edges"]]
    assert nodes[0]["firstName"] is None and nodes[0]["dob"] == "1990"
    ext = json.loads(nodes[0]["jsonExt"])
    assert ext == {
        "national_id": {"verified": True},
        "income": True,
        "household_size": 4,
        "address": {"district": "North"},
    }
    assert json.loads(nodes[1]["jsonExt"])["income"] is False
    assert "NID-1001" not in r.text and "Market Lane" not in r.text


def test_purpose_required_and_role_gates(gateway):
    r = gateway.post(
        "/api/graphql",
        json={"query": "query { individualExport { edges { node { firstName } } } }"},
        headers=CW,
    )
    assert r.status_code == 403 and r.json()["reason_codes"] == ["ROLE_NOT_PERMITTED"]
    r = gateway.post("/api/graphql", json=QUERY, headers={"X-Purpose": "registration"})
    assert r.status_code == 401
    r = gateway.post("/api/graphql", json=QUERY, headers={**CW, "X-Purpose": "no_such_purpose"})
    assert r.status_code == 403 and r.json()["reason_codes"] == ["UNKNOWN_PURPOSE"]


def test_unmapped_operations_and_paths_pass_through(gateway):
    r = gateway.post("/api/graphql", json={"query": "query { unmapped }"})
    assert r.status_code == 200 and r.json()["data"]["secret"] == "still-there"
    assert gateway.get("/api/other").json() == {"passthrough": True}


def test_fhir_patient_is_filtered_and_audited(gateway, harness):
    r = gateway.get("/api/api_fhir_r4/Patient/p-1", headers={**CW, "X-Purpose": "eligibility_verification"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] is None and body["birthDate"] == "1990" and body["identifier"] == {"verified": True}
    assert body["address"] == [{"district": "North"}] and body["telecom"] is None
    aud = harness.headers(sub="aud-1", role="AUDITOR")
    events = (
        harness.client("audit")
        .get("/v1/events", headers=aud, params={"event_type": "read_access"})
        .json()["events"]
    )
    assert events and events[0]["details"]["entity"] == "Patient" and events[0]["details"]["subjects"] == 1


def test_gateway_fails_closed_without_control_plane():
    cp = PrivacyControlPlane("http://127.0.0.1:9", token="x", timeout=0.2)
    cfg = GatewayConfig(
        upstream="http://upstream",
        mapping=Mapping.load(ROOT / "integrations/openimis/mapping.yaml"),
        control_plane=cp,
    )
    app = create_gateway(cfg, upstream_transport=InProcessTransport(fake_upstream()))
    with httpx.Client(transport=InProcessTransport(app), base_url="http://gw") as c:
        r = c.post("/api/graphql", json=QUERY, headers={**CW, "X-Purpose": "registration"})
        assert r.status_code == 503 and r.json()["error"] == "control_plane_unavailable"


def test_config_from_file(tmp_path, harness):
    cfg_file = tmp_path / "gw.yaml"
    cfg_file.write_text(f"""
upstream: http://upstream
mapping: {ROOT / "integrations/openimis/mapping.yaml"}
control_plane: {{base_url: http://cp, token: x}}
actor: {{source: jwt, jwt_secret: s3cret, static_roles: {{cw-9: CASE_WORKER}}}}
""")
    cfg = GatewayConfig.from_file(cfg_file)
    assert (
        cfg.actor_source == "jwt"
        and cfg.static_roles == {"cw-9": "CASE_WORKER"}
        and cfg.mapping.default_program
    )
