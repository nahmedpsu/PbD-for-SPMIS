"""The end-to-end privacy walkthrough used by ``pbd-spmis demo``.

It boots every service in-process against fresh databases and narrates one beneficiary's
journey through registration, eligibility, enrollment, payment, exceptional access, audit and
retention, showing at each step what each actor can and cannot see.
"""

from __future__ import annotations

import json
import tempfile
from typing import Any

from .testing import Harness

_steps: list[dict[str, Any]] = []


def _say(title: str, body: Any = None, *, json_output: bool) -> None:
    _steps.append({"step": title, "result": body})
    if json_output:
        return
    print(f"\n== {title}")
    if body is not None:
        print(json.dumps(body, indent=2, default=str) if not isinstance(body, str) else body)


def run_demo(*, json_output: bool = False) -> list[dict[str, Any]]:
    _steps.clear()
    h = Harness(tmp_dir=tempfile.mkdtemp(prefix="pbd-demo-"))
    cw = h.headers(
        sub="cw-amara",
        role="CASE_WORKER",
        purpose="eligibility_verification",
        program="cash_assistance",
        programs=["cash_assistance"],
    )

    _say(
        "1. Registration officer proofs identity and registers the household (fictional data)",
        json_output=json_output,
    )
    person = h.register_person(
        national_id="NID-1001",
        name="Amina Example",
        contact="+100000001",
        income=180,
        household_size=4,
        disability_status="not_certified",
        employment_status="informal",
        district="North",
        region="Northern",
        street="12 Market Lane",
    )
    tok = person["person_token"]
    _say(
        "   -> the vault issued an opaque person token; no other store ever sees the national id",
        {"person_token": tok, "household_token": person["household_token"]},
        json_output=json_output,
    )

    _say(
        "2. Case worker verifies eligibility: the broker asks external sources for assertions",
        json_output=json_output,
    )
    det = (
        h.client("eligibility").post("/v1/eligibility/verify", headers=cw, json={"person_token": tok}).json()
    )
    _say(
        "   -> determination (query, do not copy: only threshold results and provenance are stored)",
        det,
        json_output=json_output,
    )

    _say(
        "3. What the case worker sees in the registry under eligibility_verification", json_output=json_output
    )
    view = h.client("registry").get(
        f"/v1/persons/{tok}",
        headers=cw,
        params={"attributes": "income,address,household_size,disability_status"},
    )
    _say(
        "   -> income became a threshold assertion, address a district, obligations attached",
        {
            "attributes": view.json()["attributes"],
            "release": view.json()["release"],
            "obligations": view.headers.get("x-obligations"),
        },
        json_output=json_output,
    )

    low = h.client("registry").get(
        f"/v1/persons/{tok}",
        headers={**cw, "X-Device-Trust": "low"},
        params={"attributes": "income,household_size"},
    )
    _say(
        "   -> the same worker on a low-trust device: sensitive attributes are withheld",
        low.json()["attributes"],
        json_output=json_output,
    )

    wrong = h.client("registry").get(
        f"/v1/persons/{tok}",
        headers={**cw, "X-Purpose": "analytics_reporting"},
        params={"attributes": "income"},
    )
    _say("   -> the same worker under a purpose the role may not use", wrong.json(), json_output=json_output)

    _say(
        "4. Enrollment issues a program-specific identifier and computes the entitlement",
        json_output=json_output,
    )
    enr = (
        h.client("program")
        .post(
            "/v1/enrollments",
            headers={**cw, "X-Purpose": "enrollment"},
            json={"person_token": tok, "determination_id": det["determination_id"]},
        )
        .json()
    )
    _say(
        "   -> program store record (keyed by program id, not person token or national id)",
        enr,
        json_output=json_output,
    )
    ppid = enr["program_person_id"]

    _say(
        "5. Payment: instrument tokenised, instruction executed by the payment service",
        json_output=json_output,
    )
    officer = h.headers(
        sub="officer-1",
        role="REGISTRATION_OFFICER",
        purpose="registration",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    inst = (
        h.client("payments")
        .post(
            "/v1/instruments",
            headers=officer,
            json={"person_token": tok, "account_number": "12345678", "bank_code": "BNK1"},
        )
        .json()
    )
    pay = h.headers(
        sub="svc:payments-batch",
        role="PAYMENT_SERVICE",
        purpose="payment_execution",
        program="cash_assistance",
        programs=["*"],
        svc=True,
    )
    ins = h.client("payments").post("/v1/instructions", headers=pay, json={"program_person_id": ppid}).json()
    ex = h.client("payments").post(f"/v1/instructions/{ins['instruction_id']}/execute", headers=pay).json()
    status = (
        h.client("payments")
        .get(
            f"/v1/instructions/{ins['instruction_id']}", headers={**cw, "X-Purpose": "payment_status_inquiry"}
        )
        .json()
    )
    _say(
        "   -> instrument token / execution result / what a case worker sees afterwards",
        {"instrument": inst, "execution": ex, "case_worker_view": status["attributes"]},
        json_output=json_output,
    )

    _say(
        "6. Break-glass: emergency contact under an approved, time-bounded, reviewed grant",
        json_output=json_output,
    )
    req = h.headers(sub="cw-amara", role="CASE_WORKER", mfa=True)
    grant = (
        h.client("breakglass")
        .post(
            "/v1/grants",
            headers=req,
            json={
                "subject_token": tok,
                "program": "cash_assistance",
                "attributes": ["contact", "name"],
                "reason_code": "immediate_risk",
                "reason": "Flood warning in the district; household must be warned tonight.",
                "duration_minutes": 30,
            },
        )
        .json()
    )
    sup = h.headers(sub="sup-chen", role="SUPERVISOR", mfa=True)
    h.client("breakglass").post(f"/v1/grants/{grant['grant_id']}/approve", headers=sup)
    emergency = h.headers(
        sub="cw-amara",
        role="CASE_WORKER",
        purpose="emergency_protection",
        program="cash_assistance",
        programs=["cash_assistance"],
        mfa=True,
        break_glass_grant=grant["grant_id"],
    )
    resolved = h.client("vault").post(
        "/v1/resolve",
        headers=emergency,
        json={"person_token": tok, "attributes": ["contact", "name", "national_id"]},
    )
    h.client("breakglass").post(f"/v1/grants/{grant['grant_id']}/revoke", headers=sup)
    after = h.client("vault").post(
        "/v1/resolve", headers=emergency, json={"person_token": tok, "attributes": ["contact"]}
    )
    _say(
        "   -> released exactly the granted attributes (national id out of scope); revoked grant fails",
        {
            "during_grant": resolved.json()["attributes"],
            "obligations": resolved.headers.get("x-obligations"),
            "after_revoke": after.json(),
        },
        json_output=json_output,
    )

    _say("7. Analyst export is de-identified and k-anonymous", json_output=json_output)
    for i in range(1, 6):
        h.register_person(national_id=f"NID-300{i}", name=f"Person {i}", income=100 + i, household_size=3)
    analyst = h.headers(
        sub="an-1",
        role="ANALYST",
        purpose="analytics_reporting",
        program="cash_assistance",
        programs=["cash_assistance"],
    )
    exp = (
        h.client("registry")
        .get("/v1/export", headers=analyst, params={"attributes": "income,address,household_size"})
        .json()
    )
    _say(
        "   -> rows are bands and regions; small groups are suppressed",
        {"count": exp["count"], "suppressed": exp["suppressed_for_k_anonymity"], "sample": exp["rows"][:2]},
        json_output=json_output,
    )

    _say("8. Audit: tamper-evident chain and the privacy operations dashboard", json_output=json_output)
    aud = h.headers(sub="aud-1", role="AUDITOR")
    chain = h.client("audit").get("/v1/chain/verify", headers=aud).json()
    metrics = h.client("audit").get("/v1/metrics", headers=aud).json()
    _say("   -> chain / metrics", {"chain": chain, "metrics": metrics}, json_output=json_output)

    _say("9. Retention: what expires, then executing the end actions", json_output=json_output)
    pa = h.headers(sub="pa-1", role="PRIVACY_ADMIN")
    far = "2040-01-01T00:00:00+00:00"
    dry = h.client("retention").post("/v1/run", headers=pa, json={"dry_run": True, "as_of": far}).json()
    run = h.client("retention").post("/v1/run", headers=pa, json={"dry_run": False, "as_of": far}).json()
    _say(
        "   -> dry run summary / executed summary",
        {
            "dry_run": dry["summary"],
            "executed": run["summary"],
            "actions": sorted({(i["record_type"], i["result"]) for i in run["items"]}),
        },
        json_output=json_output,
    )
    gone = h.client("registry").get(
        f"/v1/persons/{tok}", headers=cw, params={"attributes": "income,household_size,address"}
    )
    _say(
        "   -> the registry record after anonymisation",
        gone.json().get("attributes"),
        json_output=json_output,
    )

    if json_output:
        print(json.dumps(_steps, indent=2, default=str))
    else:
        print("\nDone. Every step above was a policy decision recorded in the audit chain.")
    h.close()
    return list(_steps)
