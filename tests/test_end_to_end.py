"""End-to-end flows through every service, exercising the privacy tests of guide section 14.1."""

from __future__ import annotations

import json
import sqlite3

import pytest


def _cw(h, purpose="eligibility_verification", **kw):
    return h.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose=purpose,
        program="cash_assistance",
        programs=["cash_assistance"],
        **kw,
    )


@pytest.fixture()
def amina(harness):
    return harness.register_person(
        national_id="NID-1001",
        name="Amina Example",
        contact="+100000001",
        income=180,
        household_size=4,
        disability_status="not_certified",
        employment_status="informal",
        district="North",
        region="Northern",
    )


def test_identity_proofing_is_deduplicated_and_vault_holds_only_ciphertext(harness, amina, tmp_path):
    officer = harness.headers(
        sub="officer-2",
        role="REGISTRATION_OFFICER",
        purpose="identity_proofing",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    again = harness.client("vault").post(
        "/v1/identities",
        headers=officer,
        json={"national_id": "nid 1001", "name": "Amina Example", "contact": "+100000001"},
    )
    assert again.status_code == 200 and again.json() == {
        "person_token": amina["person_token"],
        "created": False,
    }
    con = sqlite3.connect(tmp_path / "vault.sqlite3")
    rows = con.execute("select national_id_idx, national_id_ct, name_ct from identities").fetchall()
    assert len(rows) == 1
    idx, nid_ct, name_ct = rows[0]
    assert b"NID-1001" not in nid_ct and b"Amina" not in name_ct and "NID" not in idx
    # The registry database never sees a name or identifier either.
    reg = sqlite3.connect(tmp_path / "registry.sqlite3")
    dump = "\n".join(reg.iterdump())
    assert "NID-1001" not in dump and "Amina" not in dump


def test_minimisation_case_worker_sees_assertions_not_values(harness, amina):
    tok = amina["person_token"]
    r = harness.client("registry").get(
        f"/v1/persons/{tok}",
        headers=_cw(harness),
        params={"attributes": "income,address,household_size,disability_status"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attributes"] == {
        "income": True,
        "address": {"district": "North"},
        "household_size": 4,
        "disability_status": False,
    }
    assert body["release"]["income"] == "assertion:below_threshold"
    assert "no_export" in r.headers["x-obligations"].split(",")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-decision-id"].startswith("PD-")


def test_purpose_enforcement_same_actor_same_field_different_purpose(harness, amina):
    tok = amina["person_token"]
    allowed = harness.client("registry").get(
        f"/v1/persons/{tok}", headers=_cw(harness), params={"attributes": "income"}
    )
    assert allowed.status_code == 200 and allowed.json()["attributes"]["income"] is True
    denied = harness.client("registry").get(
        f"/v1/persons/{tok}",
        headers=_cw(harness, purpose="grievance_handling"),
        params={"attributes": "income"},
    )
    assert denied.status_code == 403
    assert denied.json()["reason_codes"] == ["NOTHING_RELEASABLE"]
    not_permitted = harness.client("registry").get(
        f"/v1/persons/{tok}",
        headers=_cw(harness, purpose="analytics_reporting"),
        params={"attributes": "income"},
    )
    assert not_permitted.json()["reason_codes"] == ["ROLE_NOT_PERMITTED"]


def test_cross_program_isolation(harness, amina):
    tok = amina["person_token"]
    other = harness.headers(
        sub="cw-2",
        role="CASE_WORKER",
        purpose="eligibility_verification",
        program="disability_allowance",
        programs=["disability_allowance"],
    )
    r = harness.client("registry").get(
        f"/v1/persons/{tok}", headers=other, params={"attributes": "household_size"}
    )
    assert r.status_code == 403
    assert r.json()["reason_codes"] == ["NO_SUBJECT_RELATIONSHIP"]


def test_low_trust_device_withholds_sensitive_attributes(harness, amina):
    tok = amina["person_token"]
    r = harness.client("registry").get(
        f"/v1/persons/{tok}",
        headers=_cw(harness, device_trust="low"),
        params={"attributes": "income,household_size"},
    )
    assert r.status_code == 200
    assert r.json()["attributes"] == {"household_size": 4}


def test_eligibility_queries_do_not_copy_and_store_provenance(harness, amina, tmp_path):
    tok = amina["person_token"]
    r = harness.client("eligibility").post(
        "/v1/eligibility/verify", headers=_cw(harness), json={"person_token": tok}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "eligible"
    assert body["result"]["income_threshold"]["met"] is True
    assert body["result"]["income_threshold"]["source"] == "tax-authority"
    assert body["result"]["identity_status"]["verified"] is True
    assert "raw_income_never_returned" not in json.dumps(body)
    # second call is served from the broker cache within the TTL
    r2 = harness.client("eligibility").post(
        "/v1/eligibility/verify", headers=_cw(harness), json={"person_token": tok}
    )
    assert r2.json()["result"]["income_threshold"]["cached"] is True
    # the external disclosure is audited with the keys returned, never the values
    aud = harness.headers(sub="aud-1", role="AUDITOR")
    ev = (
        harness.client("audit")
        .get("/v1/events", headers=aud, params={"event_type": "external_disclosure"})
        .json()
    )
    sources = {e["details"]["source"] for e in ev["events"]}
    assert sources == {"tax-authority", "civil-registry"}
    assert all("keys_returned" in e["details"] for e in ev["events"])
    # and the national id never landed in any non-vault database
    for db in ("broker", "eligibility", "audit", "registry"):
        dump = "\n".join(sqlite3.connect(tmp_path / f"{db}.sqlite3").iterdump())
        assert "NID-1001" not in dump, db


def test_ineligible_and_incomplete_outcomes(harness):
    rich = harness.register_person(national_id="NID-1002", name="Bo Example", income=900, household_size=1)
    r = harness.client("eligibility").post(
        "/v1/eligibility/verify", headers=_cw(harness), json={"person_token": rich["person_token"]}
    )
    assert r.json()["outcome"] == "ineligible"
    unknown = harness.register_person(
        national_id="NID-9999", name="Nobody Example", income=10, household_size=1
    )
    r = harness.client("eligibility").post(
        "/v1/eligibility/verify", headers=_cw(harness), json={"person_token": unknown["person_token"]}
    )
    assert r.json()["outcome"] == "incomplete"
    assert r.json()["result"]["income_threshold"]["status"] == "no_record"


def test_enrollment_payment_and_status_views(harness, amina):
    tok = amina["person_token"]
    det = (
        harness.client("eligibility")
        .post("/v1/eligibility/verify", headers=_cw(harness), json={"person_token": tok})
        .json()
    )
    enr = harness.client("program").post(
        "/v1/enrollments",
        headers=_cw(harness, purpose="enrollment"),
        json={"person_token": tok, "determination_id": det["determination_id"]},
    )
    assert enr.status_code == 201, enr.text
    ppid = enr.json()["program_person_id"]
    assert ppid.startswith("CASH-")
    assert enr.json()["entitlement"] == {"amount": 70.0, "currency": "XSP", "period": "monthly"}

    officer = harness.headers(
        sub="officer-1",
        role="REGISTRATION_OFFICER",
        purpose="registration",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    inst = harness.client("payments").post(
        "/v1/instruments",
        headers=officer,
        json={"person_token": tok, "account_number": "12345678", "bank_code": "BNK1"},
    )
    assert inst.status_code == 201

    pay = harness.headers(
        sub="svc:payments-batch",
        role="PAYMENT_SERVICE",
        purpose="payment_execution",
        program="cash_assistance",
        programs=["*"],
        svc=True,
    )
    ins = harness.client("payments").post("/v1/instructions", headers=pay, json={"program_person_id": ppid})
    assert ins.status_code == 201, ins.text
    iid = ins.json()["instruction_id"]
    ex = harness.client("payments").post(f"/v1/instructions/{iid}/execute", headers=pay)
    assert ex.status_code == 200 and ex.json()["status"] == "executed", ex.text
    assert ex.json()["provider_reference"].startswith("TXN-")

    # A case worker sees status, amount and exception reason; the instrument is denied entirely.
    view = harness.client("payments").get(
        f"/v1/instructions/{iid}", headers=_cw(harness, purpose="payment_status_inquiry")
    )
    assert view.status_code == 200
    assert set(view.json()["attributes"]) == {"payment_status"}
    assert view.json()["attributes"]["payment_status"]["amount"] == 70.0

    # A finance officer under payment_execution is overridden to deny the instrument as well.
    fin = harness.headers(
        sub="fin-1",
        role="FINANCE_OFFICER",
        purpose="payment_execution",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    fview = harness.client("payments").get(f"/v1/instructions/{iid}", headers=fin)
    assert "bank_account" not in fview.json()["attributes"]
    fexec = harness.client("payments").post(f"/v1/instructions/{iid}/execute", headers=fin)
    assert fexec.status_code == 422 and fexec.json()["error"] == "invalid_state"

    # Reconciliation closes the loop.
    rec = harness.client("payments").post(
        f"/v1/instructions/{iid}/reconcile",
        headers=pay,
        json={"status": "settled", "provider_reference": ex.json()["provider_reference"]},
    )
    assert rec.json()["status"] == "settled"

    # The enrollment view for a grievance officer shows status; the rest follows the release map.
    go = harness.headers(
        sub="go-1",
        role="GRIEVANCE_OFFICER",
        purpose="grievance_handling",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    e = harness.client("program").get(f"/v1/enrollments/{ppid}", headers=go)
    assert e.status_code == 200 and e.json()["attributes"]["enrollment_status"] == "enrolled"


def test_enrollment_refused_without_eligibility(harness):
    rich = harness.register_person(national_id="NID-1002", name="Bo Example", income=900, household_size=1)
    det = (
        harness.client("eligibility")
        .post("/v1/eligibility/verify", headers=_cw(harness), json={"person_token": rich["person_token"]})
        .json()
    )
    enr = harness.client("program").post(
        "/v1/enrollments",
        headers=_cw(harness, purpose="enrollment"),
        json={"person_token": rich["person_token"], "determination_id": det["determination_id"]},
    )
    assert enr.status_code == 403 and enr.json()["error"] == "not_eligible"


def test_token_unlinkability(harness, amina):
    tok = amina["person_token"]
    program_svc = harness.headers(
        sub="svc:program",
        role="PROGRAM_SERVICE",
        purpose="token_resolution",
        program="cash_assistance",
        programs=["*"],
        svc=True,
    )
    r = harness.client("vault").post(
        "/v1/resolve", headers=program_svc, json={"person_token": tok, "attributes": ["national_id"]}
    )
    assert r.status_code == 403 and r.json()["reason_codes"] == ["ROLE_NOT_PERMITTED"]
    human = _cw(harness, purpose="token_resolution")
    r = harness.client("vault").post(
        "/v1/resolve", headers=human, json={"person_token": tok, "attributes": ["national_id"]}
    )
    assert r.status_code == 403
    # case management shows a masked id only, and only on an assigned case
    cm = harness.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose="case_management",
        program="cash_assistance",
        programs=["cash_assistance"],
        cases=["CASE-1"],
        case_id="CASE-1",
    )
    r = harness.client("vault").post(
        "/v1/resolve", headers=cm, json={"person_token": tok, "attributes": ["national_id", "name"]}
    )
    assert r.status_code == 200
    assert r.json()["attributes"]["national_id"] == "******01"
    assert r.json()["attributes"]["name"] == "Amina Example"


def test_export_is_deidentified_and_k_anonymous(harness):
    for i in range(1, 6):
        harness.register_person(
            national_id=f"NID-300{i}",
            name=f"Person {i}",
            income=100 + i,
            household_size=3,
            district="North",
            region="Northern",
        )
    harness.register_person(
        national_id="NID-1002",
        name="Outlier",
        income=900,
        household_size=9,
        district="Central",
        region="Central",
    )
    analyst = harness.headers(
        sub="an-1",
        role="ANALYST",
        purpose="analytics_reporting",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    r = harness.client("registry").get(
        "/v1/export", headers=analyst, params={"attributes": "income,address,household_size"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["k"] == 5 and body["suppressed_for_k_anonymity"] == 1 and body["count"] == 5
    assert body["rows"][0] == {"income": "100-249", "address": {"region": "Northern"}, "household_size": 3}
    # the same actor may not export under an operational purpose, and a case worker never may
    r = harness.client("registry").get(
        "/v1/export", headers=_cw(harness), params={"attributes": "household_size"}
    )
    assert r.status_code == 403 and r.json()["reason_codes"] == ["ACTION_NOT_PERMITTED"]
    aud = harness.headers(sub="aud-1", role="AUDITOR")
    assert harness.client("audit").get("/v1/metrics", headers=aud).json()["exports"] == 1


def test_case_management_relationship_based_access(harness, amina):
    tok = amina["person_token"]
    det = (
        harness.client("eligibility")
        .post("/v1/eligibility/verify", headers=_cw(harness), json={"person_token": tok})
        .json()
    )
    ppid = (
        harness.client("program")
        .post(
            "/v1/enrollments",
            headers=_cw(harness, purpose="enrollment"),
            json={"person_token": tok, "determination_id": det["determination_id"]},
        )
        .json()["program_person_id"]
    )
    assigned = harness.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose="case_management",
        program="cash_assistance",
        programs=["cash_assistance"],
        cases=["CASE-A1"],
        case_id="CASE-A1",
    )
    c = harness.client("program").post(
        "/v1/cases",
        headers=assigned,
        json={
            "program_person_id": ppid,
            "case_id": "CASE-A1",
            "sensitivity": "restricted",
            "narrative": "Household reports intimidation by a neighbour.",
        },
    )
    assert c.status_code == 201, c.text
    ok = harness.client("program").get("/v1/cases/CASE-A1", headers=assigned)
    assert ok.status_code == 200 and "intimidation" in ok.json()["attributes"]["case_narrative"]
    stranger = harness.headers(
        sub="cw-7",
        role="CASE_WORKER",
        purpose="case_management",
        program="cash_assistance",
        programs=["cash_assistance"],
        cases=["CASE-Z9"],
        case_id="CASE-A1",
    )
    denied = harness.client("program").get("/v1/cases/CASE-A1", headers=stranger)
    assert denied.status_code == 403 and denied.json()["reason_codes"] == ["NO_CASE_ASSIGNMENT"]
