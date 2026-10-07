"""Tests for the openIMIS backend module, run against a graphene schema shaped like openIMIS's
Individual/Beneficiary types and the real in-process control plane (no openIMIS needed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import graphene
import pytest
from graphene.types.generic import GenericScalar

from pbd_spmis.common.clients import InProcessTransport
from pbd_spmis.sdk import PrivacyControlPlane
from pbd_spmis.sdk.mapping import Mapping

ROOT = Path(__file__).resolve().parents[1]
MODULE_DIR = ROOT / "integrations" / "openimis" / "openimis-be-pbd_py"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from pbd.config import configure  # noqa: E402
from pbd.middleware import PrivacyMiddleware  # noqa: E402

# ---------------------------------------------------------------------------- fake openIMIS schema

INDIVIDUALS = [
    SimpleNamespace(
        uuid="11111111-1111-1111-1111-111111111111",
        first_name="Amina",
        last_name="Example",
        dob="1990-05-04",
        json_ext={
            "national_id": "NID-1001",
            "email": "amina@example.test",
            "income": 180,
            "household_size": 4,
            "disability_status": "not_certified",
            "address": {"district": "North", "street": "12 Market Lane"},
            "custom_flag": "keep-me",
        },
    ),
    SimpleNamespace(
        uuid="22222222-2222-2222-2222-222222222222",
        first_name="Bo",
        last_name="Example",
        dob="1975-01-30",
        json_ext={"national_id": "NID-1002", "income": 900, "household_size": 1},
    ),
]
CREATED: list[dict] = []


class IndividualGQLType(graphene.ObjectType):
    uuid = graphene.String()
    first_name = graphene.String()
    last_name = graphene.String()
    dob = graphene.String()
    json_ext = GenericScalar()


class BeneficiaryGQLType(graphene.ObjectType):
    id = graphene.String()
    status = graphene.String()
    individual = graphene.Field(IndividualGQLType)


class Query(graphene.ObjectType):
    individual = graphene.List(IndividualGQLType, benefit_plan_code=graphene.String())
    beneficiary = graphene.List(BeneficiaryGQLType)
    benefit_plan = graphene.List(graphene.String)
    unmapped = graphene.String()

    def resolve_individual(root, info, benefit_plan_code=None):
        return INDIVIDUALS

    def resolve_beneficiary(root, info):
        return [SimpleNamespace(id="b-1", status="ACTIVE", individual=INDIVIDUALS[0])]

    def resolve_benefit_plan(root, info):
        return ["CASH"]

    def resolve_unmapped(root, info):
        return "visible"


class IndividualInput(graphene.InputObjectType):
    first_name = graphene.String()
    last_name = graphene.String()
    dob = graphene.String()
    json_ext = GenericScalar()


class CreateIndividual(graphene.Mutation):
    class Arguments:
        input = IndividualInput(required=True)

    ok = graphene.Boolean()
    stored_json_ext = GenericScalar()

    def mutate(root, info, input):
        CREATED.append(dict(input))
        return CreateIndividual(ok=True, stored_json_ext=dict(input.get("json_ext") or {}))


class Mutation(graphene.ObjectType):
    create_individual = CreateIndividual.Field()


SCHEMA = graphene.Schema(query=Query, mutation=Mutation, auto_camelcase=True)


def fake_request(*, rights=(170001,), headers=None, username="cw-1"):
    meta = {("HTTP_" + k.upper().replace("-", "_")): v for k, v in (headers or {}).items()}
    user = SimpleNamespace(username=username, rights=list(rights), is_authenticated=True)
    return SimpleNamespace(META=meta, user=user)


@pytest.fixture()
def module(harness):
    cp = PrivacyControlPlane(
        "http://cp",
        token=lambda: harness.token(sub="svc:openimis", role="REGISTRY_SERVICE", programs=["*"], svc=True),
        transport=InProcessTransport(harness.app),
    )
    cfg = configure(
        {"vault_identifiers": False},
        control_plane=cp,
        mapping=Mapping.load(ROOT / "integrations/openimis/mapping.yaml"),
    )
    yield cfg
    cp.close()


def run(query, request, cfg, variables=None):
    return SCHEMA.execute(
        query, context_value=request, middleware=[PrivacyMiddleware(cfg)], variable_values=variables
    )


INDIVIDUAL_QUERY = """
query { individual(benefitPlanCode: "CASH") { uuid firstName lastName dob jsonExt } }
"""


def test_case_worker_under_eligibility_purpose_gets_minimised_view(module):
    res = run(INDIVIDUAL_QUERY, fake_request(headers={"X-Purpose": "eligibility_verification"}), module)
    assert res.errors is None, res.errors
    amina, bo = res.data["individual"]
    assert amina["uuid"].startswith("1111")
    assert amina["firstName"] is None and amina["lastName"] is None  # name denied for this purpose
    assert amina["dob"] == "1990"
    assert amina["jsonExt"] == {
        "national_id": {"verified": True},
        "income": True,
        "household_size": 4,
        "disability_status": False,
        "address": {"district": "North"},
        "custom_flag": "keep-me",
    }
    assert bo["jsonExt"]["income"] is False


def test_operation_default_purpose_applies_when_header_absent(module):
    # mapping default for `individual` is registration; a registration officer sees exact values
    res = run(INDIVIDUAL_QUERY, fake_request(rights=(159002,), username="officer-1"), module)
    assert res.errors is None, res.errors
    amina = res.data["individual"][0]
    assert amina["firstName"] == "Amina" and amina["dob"] == "1990-05-04"
    assert amina["jsonExt"]["national_id"] == {"verified": True}  # registration reads back verify-only
    assert amina["jsonExt"]["income"] == 180


def test_role_not_permitted_for_purpose_errors_the_field(module):
    res = run(INDIVIDUAL_QUERY, fake_request(headers={"X-Purpose": "analytics_reporting"}), module)
    assert res.errors and "ROLE_NOT_PERMITTED" in str(res.errors[0])


def test_unknown_rights_cannot_derive_a_role(module):
    res = run(INDIVIDUAL_QUERY, fake_request(rights=(999999,), headers={"X-Purpose": "registration"}), module)
    assert res.errors and "no PbD role" in str(res.errors[0])


def test_unmapped_operations_pass_through(module):
    res = run("query { unmapped benefitPlan }", fake_request(), module)
    assert res.errors is None and res.data == {"unmapped": "visible", "benefitPlan": ["CASH"]}


def test_low_trust_device_and_nested_individual_in_beneficiary(module):
    q = "query { beneficiary { id status individual { firstName jsonExt } } }"
    res = run(
        q, fake_request(headers={"X-Purpose": "eligibility_verification", "X-Device-Trust": "low"}), module
    )
    assert res.errors is None, res.errors
    b = res.data["beneficiary"][0]
    assert b["status"] is None  # enrollment_status not released under eligibility_verification
    assert b["individual"]["jsonExt"]["household_size"] == 4
    assert "income" not in b["individual"]["jsonExt"]  # C4 withheld on a low-trust device


def test_reads_are_audited_once_per_entity(module, harness):
    run(
        INDIVIDUAL_QUERY,
        fake_request(headers={"X-Purpose": "eligibility_verification", "X-Correlation-ID": "corr-1"}),
        module,
    )
    aud = harness.headers(sub="aud-1", role="AUDITOR")
    events = (
        harness.client("audit")
        .get("/v1/events", headers=aud, params={"event_type": "read_access"})
        .json()["events"]
    )
    assert len(events) == 1
    assert events[0]["details"]["entity"] == "Individual" and events[0]["correlation_id"] == "corr-1"
    assert events[0]["purpose"] == "eligibility_verification"


def test_vaulting_moves_identifier_out_of_openimis(harness):
    cp = PrivacyControlPlane(
        "http://cp",
        token=lambda: harness.token(sub="svc:openimis", role="REGISTRY_SERVICE", programs=["*"], svc=True),
        transport=InProcessTransport(harness.app),
    )
    cfg = configure(
        {"vault_identifiers": True},
        control_plane=cp,
        mapping=Mapping.load(ROOT / "integrations/openimis/mapping.yaml"),
    )
    CREATED.clear()
    mutation = """
    mutation { createIndividual(input: {firstName: "Amina", lastName: "Example", dob: "1990-05-04",
      jsonExt: {national_id: "NID-1001", email: "amina@example.test", income: 180}}) { ok storedJsonExt } }
    """
    res = SCHEMA.execute(
        mutation,
        context_value=fake_request(rights=(159002,), username="officer-1"),
        middleware=[PrivacyMiddleware(cfg)],
    )
    assert res.errors is None, res.errors
    stored = res.data["createIndividual"]["storedJsonExt"]
    assert stored["national_id"] == "vaulted" and stored["email"] == "vaulted"
    assert stored["pbd_person_token"].startswith("P-") and stored["income"] == 180
    assert "NID-1001" not in json.dumps(CREATED)
    # the vault holds the identity and dedups it
    again = cp.vault.tokenize(national_id="NID-1001", name="Amina Example", program="cash_assistance")
    assert again["person_token"] == stored["pbd_person_token"] and again["created"] is False
    cp.close()


def test_fail_closed_when_control_plane_unreachable():
    cp = PrivacyControlPlane("http://127.0.0.1:9", token="x", timeout=0.2)
    cfg = configure({}, control_plane=cp, mapping=Mapping.load(ROOT / "integrations/openimis/mapping.yaml"))
    res = run(INDIVIDUAL_QUERY, fake_request(headers={"X-Purpose": "registration"}), cfg)
    assert res.errors and "failing closed" in str(res.errors[0])
    assert res.data["individual"][0]["firstName"] is None


# ---------------------------------------------------------------------- behaviours learnt from real openIMIS


class StrictIndividualGQLType(graphene.ObjectType):
    """Shaped like openIMIS's real type: non-null strings, a Date, a non-null JSON container."""

    class Meta:
        name = "IndividualGQLType"  # the mapping keys on openIMIS's type name

    uuid = graphene.String()
    first_name = graphene.NonNull(graphene.String)
    last_name = graphene.NonNull(graphene.String)
    dob = graphene.Date()
    json_ext = graphene.NonNull(GenericScalar)


class StrictQuery(graphene.ObjectType):
    individual = graphene.List(StrictIndividualGQLType)

    def resolve_individual(root, info):
        import datetime

        return [
            SimpleNamespace(
                uuid="3333",
                first_name="Amina",
                last_name="Example",
                dob=datetime.date(1990, 5, 4),
                json_ext={"national_id": "NID-1001", "income": 180},
            )
        ]


STRICT_SCHEMA = graphene.Schema(query=StrictQuery, auto_camelcase=True)


def test_non_null_and_typed_fields_as_in_real_openimis(module):
    res = STRICT_SCHEMA.execute(
        "query { individual { uuid firstName lastName dob jsonExt } }",
        context_value=fake_request(headers={"X-Purpose": "eligibility_verification"}),
        middleware=[PrivacyMiddleware(module)],
    )
    assert res.errors is None, res.errors
    node = res.data["individual"][0]
    assert node["firstName"] == "***" and node["lastName"] == "***"  # String! cannot be null
    assert node["dob"] == "1990-01-01"  # Date field, year precision
    assert node["jsonExt"] == {"national_id": {"verified": True}, "income": True}


def test_promise_results_are_transformed():
    from pbd.middleware import _then

    class FakePromise:
        def __init__(self, value):
            self.value = value

        def then(self, fn):
            return FakePromise(fn(self.value))

    assert _then(FakePromise(5), lambda v: v + 1).value == 6
    assert _then(5, lambda v: v + 1) == 6


# ---------------------------------------------------------------------- relationship resolver and migration


def test_relationship_resolver_maps_benefit_plan_codes(module):
    from pbd.relationships import from_beneficiaries

    class Individual:  # plain object with explicit codes; the ORM path runs in the openIMIS validation
        pbd_benefit_plan_codes = ["CASH", "DISA", "UNKNOWN"]

    assert from_beneficiaries(Individual()) == ["cash_assistance", "disability_allowance"]

    class Beneficiary:
        pbd_benefit_plan_codes = []

    assert from_beneficiaries(Beneficiary()) == []


def test_vault_record_rewrites_json_ext_and_is_idempotent():
    from pbd.migration import migrate_individuals, vault_record

    calls = []

    def tokenize(**kw):
        calls.append(kw)
        return {"person_token": "P-MIGRATED", "created": True}

    ext = {"national_id": "NID-1002", "email": "bo@example.test", "income": 900}
    new = vault_record(ext, name="Bo Example", program="cash_assistance", vault=None, tokenize=tokenize)
    assert new == {
        "national_id": "vaulted",
        "email": "vaulted",
        "income": 900,
        "pbd_person_token": "P-MIGRATED",
    }
    assert calls[0]["national_id"] == "NID-1002" and calls[0]["contact"] == "bo@example.test"
    assert ext["national_id"] == "NID-1002"  # input not mutated
    assert (
        vault_record(new, name="Bo Example", program="cash_assistance", vault=None, tokenize=tokenize) is None
    )
    assert (
        vault_record({"income": 1}, name="x", program="cash_assistance", vault=None, tokenize=tokenize)
        is None
    )

    class Ind:
        def __init__(self, ext):
            self.id, self.first_name, self.last_name, self.json_ext, self.saved = (
                1,
                "Bo",
                "Example",
                ext,
                False,
            )

        def save(self):
            self.saved = True

    class FakeVault:
        class vault:  # noqa: N801 - mimics PrivacyControlPlane.vault
            @staticmethod
            def tokenize(**kw):
                return {"person_token": "P-X", "created": True}

    rows = [Ind({"national_id": "NID-1"}), Ind({"national_id": "vaulted"}), Ind({})]
    dry = migrate_individuals(
        rows, program="cash_assistance", vault=FakeVault(), dry_run=True, log=lambda m: None
    )
    assert dry == {"scanned": 3, "vaulted": 1, "skipped": 2, "failed": 0} and not rows[0].saved
    live = migrate_individuals(rows, program="cash_assistance", vault=FakeVault(), log=lambda m: None)
    assert live["vaulted"] == 1 and rows[0].saved and rows[0].json_ext["pbd_person_token"] == "P-X"
