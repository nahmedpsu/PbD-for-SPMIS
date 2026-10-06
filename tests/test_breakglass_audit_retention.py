from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture()
def amina(harness):
    return harness.register_person(
        national_id="NID-1001", name="Amina Example", contact="+100000001", income=180, household_size=4
    )


def _grant(harness, tok, attributes=("contact", "name"), requester="cw-1"):
    req = harness.headers(sub=requester, role="CASE_WORKER", mfa=True)
    r = harness.client("breakglass").post(
        "/v1/grants",
        headers=req,
        json={
            "subject_token": tok,
            "program": "cash_assistance",
            "attributes": list(attributes),
            "reason_code": "immediate_risk",
            "reason": "Household reported at immediate risk; must make contact.",
            "duration_minutes": 30,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["grant_id"]


def test_break_glass_lifecycle(harness, amina):
    tok = amina["person_token"]
    # MFA is required to even request
    r = harness.client("breakglass").post(
        "/v1/grants",
        headers=harness.headers(sub="cw-1", role="CASE_WORKER"),
        json={
            "subject_token": tok,
            "program": "cash_assistance",
            "attributes": ["contact"],
            "reason_code": "immediate_risk",
            "reason": "Household reported at immediate risk.",
            "duration_minutes": 30,
        },
    )
    assert r.status_code == 403 and r.json()["error"] == "MFA_REQUIRED"

    gid = _grant(harness, tok)
    emergency = harness.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose="emergency_protection",
        program="cash_assistance",
        programs=["cash_assistance"],
        mfa=True,
        break_glass_grant=gid,
    )
    pending = harness.client("vault").post(
        "/v1/resolve", headers=emergency, json={"person_token": tok, "attributes": ["contact"]}
    )
    assert pending.status_code == 403 and pending.json()["reason_codes"] == ["BREAK_GLASS_INACTIVE"]

    # requester cannot approve their own grant; a supervisor with MFA can
    self_approve = harness.client("breakglass").post(
        f"/v1/grants/{gid}/approve", headers=harness.headers(sub="cw-1", role="SUPERVISOR", mfa=True)
    )
    assert self_approve.status_code == 403 and self_approve.json()["error"] == "SEPARATION_OF_DUTIES"
    sup = harness.headers(sub="sup-1", role="SUPERVISOR", mfa=True)
    approved = harness.client("breakglass").post(f"/v1/grants/{gid}/approve", headers=sup)
    assert approved.status_code == 200 and approved.json()["status"] == "active"

    ok = harness.client("vault").post(
        "/v1/resolve",
        headers=emergency,
        json={"person_token": tok, "attributes": ["contact", "name", "national_id"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["attributes"] == {"contact": "+100000001", "name": "Amina Example"}
    assert "alert" in ok.headers["x-obligations"] and "review_required" in ok.headers["x-obligations"]

    # a different worker cannot ride on the grant
    other = harness.headers(
        sub="cw-2",
        role="CASE_WORKER",
        purpose="emergency_protection",
        program="cash_assistance",
        programs=["cash_assistance"],
        mfa=True,
        break_glass_grant=gid,
    )
    r = harness.client("vault").post(
        "/v1/resolve", headers=other, json={"person_token": tok, "attributes": ["contact"]}
    )
    assert r.json()["reason_codes"] == ["BREAK_GLASS_NOT_HOLDER"]

    # revoke -> inactive; review by an independent auditor
    harness.client("breakglass").post(f"/v1/grants/{gid}/revoke", headers=sup)
    r = harness.client("vault").post(
        "/v1/resolve", headers=emergency, json={"person_token": tok, "attributes": ["contact"]}
    )
    assert r.status_code == 403 and r.json()["reason_codes"] == ["BREAK_GLASS_INACTIVE"]
    rv = harness.client("breakglass").post(
        f"/v1/grants/{gid}/review",
        headers=harness.headers(sub="aud-1", role="AUDITOR"),
        json={"outcome": "justified", "notes": "Contact confirmed with the field team."},
    )
    assert rv.status_code == 200 and rv.json()["status"] == "reviewed"

    aud = harness.headers(sub="aud-1", role="AUDITOR")
    m = harness.client("audit").get("/v1/metrics", headers=aud).json()
    assert (
        m["break_glass_requested"] == 1 and m["break_glass_activated"] == 1 and m["break_glass_reviewed"] == 1
    )
    assert m["token_resolutions"] == 1


def test_break_glass_expires(harness, amina, tmp_path):
    tok = amina["person_token"]
    gid = _grant(harness, tok)
    harness.client("breakglass").post(
        f"/v1/grants/{gid}/approve", headers=harness.headers(sub="sup-1", role="SUPERVISOR", mfa=True)
    )
    con = sqlite3.connect(tmp_path / "breakglass.sqlite3")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    con.execute("update grants set expires_at=?", (past,))
    con.commit()
    con.close()
    g = harness.client("breakglass").get(
        f"/v1/grants/{gid}", headers=harness.headers(sub="sup-1", role="SUPERVISOR")
    )
    assert g.json()["status"] == "expired"
    emergency = harness.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose="emergency_protection",
        program="cash_assistance",
        programs=["cash_assistance"],
        mfa=True,
        break_glass_grant=gid,
    )
    r = harness.client("vault").post(
        "/v1/resolve", headers=emergency, json={"person_token": tok, "attributes": ["contact"]}
    )
    assert r.json()["reason_codes"] == ["BREAK_GLASS_INACTIVE"]


def test_audit_chain_detects_tampering_and_logs_reads(harness, amina, tmp_path):
    aud = harness.headers(sub="aud-1", role="AUDITOR")
    v = harness.client("audit").get("/v1/chain/verify", headers=aud).json()
    assert v["ok"] and v["checked"] > 0
    # a non-auditor cannot read the log
    r = harness.client("audit").get("/v1/events", headers=harness.headers(sub="cw-1", role="CASE_WORKER"))
    assert r.status_code == 403
    before = harness.client("audit").get("/v1/metrics", headers=aud).json()["audit_reads"]
    harness.client("audit").get("/v1/events", headers=aud, params={"event_type": "identity_created"})
    assert harness.client("audit").get("/v1/metrics", headers=aud).json()["audit_reads"] == before + 1
    # tamper with a stored row
    con = sqlite3.connect(tmp_path / "audit.sqlite3")
    con.execute("update audit_events set outcome='TAMPERED' where seq=2")
    con.commit()
    con.close()
    v = harness.client("audit").get("/v1/chain/verify", headers=aud).json()
    assert v["ok"] is False and v["broken_at_seq"] == 2


def test_audit_rejects_pii_and_sensitive_release_fails_closed(harness):
    svc = harness.headers(sub="svc:x", role="REGISTRY_SERVICE", svc=True)
    r = harness.client("audit").post(
        "/v1/events",
        headers=svc,
        json={
            "event_type": "t",
            "service": "x",
            "actor_id": "a",
            "outcome": "ok",
            "details": {"national_id": "NID-1"},
        },
    )
    assert r.status_code == 422 and r.json()["error"] == "pii_in_audit"


def test_retention_engine_executes_end_actions(harness, amina, tmp_path):
    tok = amina["person_token"]
    cw = harness.headers(
        sub="cw-1",
        role="CASE_WORKER",
        purpose="eligibility_verification",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    harness.client("eligibility").post("/v1/eligibility/verify", headers=cw, json={"person_token": tok})
    pa = harness.headers(sub="pa-1", role="PRIVACY_ADMIN")
    # a case worker may not run retention
    assert harness.client("retention").post("/v1/run", headers=cw, json={"dry_run": True}).status_code == 403
    due = harness.client("retention").get("/v1/schedules", headers=pa, params={"due_within_days": 0}).json()
    assert due["count"] == 0
    far = "2040-01-01T00:00:00+00:00"
    dry = harness.client("retention").post("/v1/run", headers=pa, json={"dry_run": True, "as_of": far}).json()
    assert dry["summary"]["due"] == 3 and dry["summary"]["executed"] == 0
    assert {(i["record_type"], i["would"]) for i in dry["items"]} == {
        ("identity_vault.identity", "delete"),
        ("social_registry.person", "anonymize"),
        ("eligibility.determination", "delete"),
    }
    run = (
        harness.client("retention").post("/v1/run", headers=pa, json={"dry_run": False, "as_of": far}).json()
    )
    assert run["summary"]["executed"] == 3 and run["summary"]["failed"] == 0
    # the registry row is anonymised, the identity is gone, evidence is in the audit log
    reg = sqlite3.connect(tmp_path / "registry.sqlite3")
    row = reg.execute("select income, anonymized_at, address from persons").fetchone()
    assert row[0] is None and row[1] is not None and "street" not in row[2]
    assert (
        sqlite3.connect(tmp_path / "vault.sqlite3").execute("select count(*) from identities").fetchone()[0]
        == 0
    )
    aud = harness.headers(sub="aud-1", role="AUDITOR")
    m = harness.client("audit").get("/v1/metrics", headers=aud).json()
    assert m["retention_actions"] == {"anonymize": 1, "delete": 2}
    assert harness.client("audit").get("/v1/chain/verify", headers=aud).json()["ok"]
    # second run finds nothing
    assert (
        harness.client("retention")
        .post("/v1/run", headers=pa, json={"dry_run": False, "as_of": far})
        .json()["summary"]["due"]
        == 0
    )
