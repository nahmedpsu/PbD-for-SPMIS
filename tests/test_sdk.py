from __future__ import annotations

import pytest

from pbd_spmis.common.clients import InProcessTransport
from pbd_spmis.sdk import PolicyDeniedError, PrivacyControlPlane, UnavailableError, apply_release, transform


@pytest.fixture()
def cp(harness):
    client = PrivacyControlPlane(
        "http://cp",
        token=lambda: harness.token(sub="svc:adapter", role="REGISTRY_SERVICE", programs=["*"], svc=True),
        transport=InProcessTransport(harness.app),
    )
    yield client
    client.close()


CASE_WORKER = {
    "id": "cw-1",
    "role": "CASE_WORKER",
    "agency": "SOCIAL_PROTECTION_AGENCY",
    "programs": ["cash_assistance"],
    "amr": ["pwd"],
}


def test_decide_and_apply_release(cp):
    d = cp.pdp.decide(
        actor=CASE_WORKER,
        program="cash_assistance",
        purpose="eligibility_verification",
        attributes=["income", "date_of_birth", "address", "bank_account"],
        subject_token="P-1",
        subject_programs=["cash_assistance"],
    )
    assert d.allow and d.decision_id.startswith("PD-")
    assert d.release == {
        "income": "assertion:below_threshold",
        "date_of_birth": "precision:year",
        "address": "precision:district",
        "bank_account": "deny",
    }
    assert d.has_obligation("expire_response") and d.obligation_value("expire_response") == "24h"
    view = cp.apply_release(
        {
            "income": 180,
            "date_of_birth": "1990-05-04",
            "address": {"district": "North", "street": "x"},
            "bank_account": "1",
        },
        d,
        program="cash_assistance",
    )
    assert view == {"income": True, "date_of_birth": "1990", "address": {"district": "North"}}


def test_enforce_raises_with_reason_codes(cp):
    with pytest.raises(PolicyDeniedError) as exc:
        cp.pdp.enforce(
            actor=CASE_WORKER,
            program="disability_allowance",
            purpose="eligibility_verification",
            attributes=["income"],
        )
    assert exc.value.reason_codes == ["NO_PROGRAM_ASSIGNMENT"]
    assert exc.value.decision_id.startswith("PD-")


def test_catalog_is_cached_and_transforms_are_local(cp):
    cat = cp.catalog()
    assert cat["version"] == "2026.10.0" and "income" in cat["attributes"]
    assert cp.catalog() is cat
    assert transform("income", 400, "band", program="cash_assistance", catalog=cat) == "250-499"
    assert (
        transform("national_id", "NID-1001", "masked", program="cash_assistance", catalog=cat) == "******01"
    )
    assert apply_release(
        {"name": "A", "income": 10}, {"name": "deny", "income": "exact"}, program="x", catalog=cat
    ) == {"income": 10}


def test_vault_tokenize_resolve_and_audit(cp, harness):
    created = cp.vault.tokenize(
        national_id="NID-1001", name="Amina Example", contact="+1", program="cash_assistance"
    )
    again = cp.vault.tokenize(national_id="nid 1001", name="Amina Example", program="cash_assistance")
    assert created["person_token"] == again["person_token"] and again["created"] is False
    tok = created["person_token"]
    # the registry service identity may not resolve identifiers
    with pytest.raises(PolicyDeniedError):
        cp.vault.resolve(
            person_token=tok,
            attributes=["national_id"],
            purpose="token_resolution",
            program="cash_assistance",
        )
    status = cp.vault.identity_status(person_token=tok, program="cash_assistance")
    assert status["verified"] is True
    eid = cp.audit.emit(
        "adapter_event", actor_id="svc:adapter", outcome="ok", details={"entity": "Individual"}
    )
    assert eid.startswith("AE-")
    with pytest.raises(Exception):  # noqa: B017 - PII guard refuses identifiers in audit payloads
        cp.audit.emit("adapter_event", actor_id="svc:adapter", outcome="ok", details={"national_id": "NID-1"})


def test_unreachable_control_plane_raises_unavailable():
    cp = PrivacyControlPlane("http://127.0.0.1:9", token="x", timeout=0.2)
    with pytest.raises(UnavailableError):
        cp.pdp.decide(
            actor=CASE_WORKER, program="cash_assistance", purpose="registration", attributes=["name"]
        )
    with pytest.raises(UnavailableError):
        PrivacyControlPlane(None).pdp.catalog()
