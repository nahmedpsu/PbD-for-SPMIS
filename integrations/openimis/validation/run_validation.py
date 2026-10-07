#!/usr/bin/env python
"""Validate openimis-be-pbd inside the real openIMIS backend (Django + graphene + PostgreSQL).

What it does
------------
1. Boots the openIMIS assembly (`openimis-be_py`) with the modules listed in ``openimis.json`` plus
   ``pbd``, using the assembly's own settings with the privacy middleware prepended.
2. Runs the openIMIS migrations against PostgreSQL.
3. Seeds a benefit plan (code CASH), two individuals with ``json_ext`` fields, beneficiaries, and
   openIMIS users whose roles carry the reference right codes (159001 search individual,
   170001 search beneficiary, 159002 create individual ...).
4. Starts the PbD control plane in-process and points the module at it.
5. Issues real GraphQL requests through the openIMIS view (``/api/graphql`` with a JWT issued by
   openIMIS) and checks the privacy behaviour: minimised views per purpose, role derivation from
   openIMIS rights, pass-through of unmapped operations, read auditing, identifier vaulting on
   ``createIndividual`` and fail-closed behaviour.

Environment (see README.md): OPENIMIS_BE_DIR, DB_* variables for PostgreSQL.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BE_DIR = Path(os.environ.get("OPENIMIS_BE_DIR", "/home/user/openimis/openimis-be_py")) / "openIMIS"

os.environ.setdefault("MODE", "pbdvalidation")
os.environ.setdefault("SITE_ROOT", "api")
os.environ.setdefault("DB_DEFAULT", "postgresql")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "5433")
os.environ.setdefault("DB_NAME", "imis")
os.environ.setdefault("DB_USER", "imis")
os.environ.setdefault("DB_PASSWORD", "imis")
os.environ.setdefault("OPENIMIS_CONF", str(HERE / "openimis.json"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openIMIS.settings")
os.environ.setdefault("PBD_MAPPING_FILE", str(HERE.parent / "mapping.yaml"))
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("ASYNC", "False")

sys.path.insert(0, str(BE_DIR))
os.chdir(BE_DIR)

# Install the settings component the assembly selects through MODE=pbdvalidation.
_component = BE_DIR / "openIMIS" / "settings" / "pbdvalidation.py"
_component.write_text((HERE / "validation_settings.py").read_text(encoding="utf-8"), encoding="utf-8")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from graphql_jwt.shortcuts import get_token  # noqa: E402

from pbd_spmis.common.clients import InProcessTransport  # noqa: E402
from pbd_spmis.sdk import PrivacyControlPlane  # noqa: E402
from pbd_spmis.testing import Harness  # noqa: E402

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: object = None) -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -> {detail}"))


def gql(client: Client, token: str, query: str, **headers: str):
    meta = {"HTTP_" + k.upper().replace("-", "_"): v for k, v in headers.items()}
    # openIMIS resolvers compare the X-CSRFToken header with a token stored in the session at login;
    # the test client has no login flow, so seed the session the way tokenAuth would.
    if "csrftoken" not in client.session:
        session = client.session
        session["csrftoken"] = "pbdvalidation"
        session.save()
    client.cookies["csrftoken"] = "pbdvalidation"
    resp = client.post(
        "/api/graphql",
        data=json.dumps({"query": query}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
        HTTP_HOST="localhost",
        HTTP_X_CSRFTOKEN="pbdvalidation",
        **meta,
    )
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {
            "errors": [{"message": "non-JSON response"}],
            "raw": resp.content[:600].decode(errors="replace"),
        }


def nodes_of(body: dict, key: str) -> list[dict]:
    data = body.get("data") or {}
    conn = data.get(key) or {}
    return [e["node"] for e in (conn.get("edges") or [])]


def ext_of(node: dict) -> dict:
    v = node.get("jsonExt")
    return v if isinstance(v, dict) else (json.loads(v) if isinstance(v, str) else {})


def main() -> int:
    print("== migrating openIMIS modules")
    call_command("migrate", verbosity=0, interactive=False)

    from core.models import User  # noqa: PLC0415
    from core.test_helpers import create_test_interactive_user, create_test_role  # noqa: PLC0415
    from individual.models import Individual  # noqa: PLC0415
    from social_protection.models import Beneficiary, BenefitPlan  # noqa: PLC0415

    print("== seeding roles, users, benefit plan, individuals")

    def ensure_role(name: str, perm_names: list[str]):
        """create_test_role keeps an existing role's rights; re-sync them so reruns are deterministic."""
        import datetime  # noqa: PLC0415

        from core.models import RoleRight  # noqa: PLC0415
        from core.test_helpers import collect_all_gql_permissions  # noqa: PLC0415
        from django.core.cache import cache  # noqa: PLC0415

        role = create_test_role(perm_names, name=name)
        flat = {k: v for app in collect_all_gql_permissions().values() for k, v in app.items()}
        wanted = {int(r) for pn in perm_names for r in flat[pn]}
        RoleRight.objects.filter(role=role).delete()
        for rid in sorted(wanted):
            RoleRight.objects.create(
                role=role, right_id=rid, audit_user_id=-1, validity_from=datetime.datetime.now()
            )
        cache.clear()
        return role

    worker_role = ensure_role(
        "PbD Case Worker",
        ["gql_individual_search_perms", "gql_beneficiary_search_perms", "gql_benefit_plan_search_perms"],
    )
    officer_role = ensure_role(
        "PbD Registration Officer",
        ["gql_individual_search_perms", "gql_individual_create_perms", "gql_individual_update_perms"],
    )
    worker = create_test_interactive_user(
        username="pbdworker", password="Pbd-Valid4tion!", roles=[worker_role.id]
    )
    officer = create_test_interactive_user(
        username="pbdofficer", password="Pbd-Valid4tion!", roles=[officer_role.id]
    )
    admin_user = User.objects.filter(username="pbdofficer").first()
    seed_user = admin_user or officer

    bp = BenefitPlan.objects.filter(code="CASH", is_deleted=False).first()
    if bp is None:
        bp = BenefitPlan(
            code="CASH",
            name="Cash Assistance",
            type="INDIVIDUAL",
            max_beneficiaries=1000,
            beneficiary_data_schema={"$id": "cash", "type": "object", "properties": {}},
            date_valid_from="2026-01-01",
        )
        bp.save(user=seed_user)

    def individual(first, last, dob, ext):
        existing = Individual.objects.filter(first_name=first, last_name=last, is_deleted=False).first()
        if existing:
            existing.json_ext = ext
            existing.save(user=seed_user)
            return existing
        obj = Individual(first_name=first, last_name=last, dob=dob, json_ext=ext)
        obj.save(user=seed_user)
        return obj

    amina = individual(
        "Amina",
        "Example",
        "1990-05-04",
        {
            "national_id": "NID-1001",
            "email": "amina@example.test",
            "income": 180,
            "household_size": 4,
            "disability_status": "not_certified",
            "address": {"district": "North", "street": "12 Market Lane"},
            "custom_flag": "keep",
        },
    )
    bo = individual(
        "Bo", "Example", "1975-01-30", {"national_id": "NID-1002", "income": 900, "household_size": 1}
    )
    for ind in (amina, bo):
        if not Beneficiary.objects.filter(individual=ind, benefit_plan=bp, is_deleted=False).exists():
            Beneficiary(
                individual=ind, benefit_plan=bp, status="ACTIVE", json_ext={}, date_valid_from="2026-01-01"
            ).save(user=seed_user)

    print("== starting the control plane in-process")
    harness = Harness(tmp_dir=tempfile.mkdtemp(prefix="pbd-openimis-"))
    cp = PrivacyControlPlane(
        "http://cp",
        transport=InProcessTransport(harness.app),
        token=lambda: harness.token(sub="svc:openimis", role="REGISTRY_SERVICE", programs=["*"], svc=True),
    )
    from pbd.config import configure  # noqa: PLC0415

    cfg = configure({"vault_identifiers": False}, control_plane=cp)
    print(f"   control plane catalogue {cp.catalog()['version']}, mapping {cfg.mapping_file}")

    client = Client()
    worker_user = User.objects.get(username="pbdworker")
    officer_user = User.objects.get(username="pbdofficer")
    rights_worker = sorted(set(worker_user.rights))
    check(
        "openIMIS user rights are the reference right codes",
        159001 in rights_worker and 170001 in rights_worker,
        rights_worker,
    )
    tw, to = get_token(worker_user), get_token(officer_user)

    q_ind = 'query { individual(lastName_Iexact: "Example", orderBy: ["firstName"]) { edges { node { uuid firstName lastName dob jsonExt } } } }'

    print("== 1. case worker, eligibility_verification purpose")
    status, body = gql(
        client,
        tw,
        q_ind,
        **{
            "X-Purpose": "eligibility_verification",
            "X-Program": "cash_assistance",
            "X-Correlation-ID": "val-1",
        },
    )
    nodes = [n for n in nodes_of(body, "individual") if n and n["uuid"] in (str(amina.uuid), str(bo.uuid))]
    check(
        "query succeeds through the openIMIS GraphQL view",
        status == 200 and not body.get("errors") and len(nodes) == 2,
        body,
    )
    if nodes:
        a = nodes[0]
        ext = ext_of(a)
        check(
            "name redacted under eligibility purpose (String! fields carry the marker)",
            a["firstName"] == "***" and a["lastName"] == "***",
            a,
        )
        check(
            "dob reduced to year precision (Date field -> first day of the year)",
            a["dob"] == "1990-01-01",
            a["dob"],
        )
        check(
            "jsonExt minimised: income -> assertion, national_id -> verify-only, address -> district, custom key kept",
            ext.get("income") is True
            and ext.get("national_id") == {"verified": True}
            and ext.get("address") == {"district": "North"}
            and ext.get("custom_flag") == "keep"
            and ext.get("household_size") == 4,
            ext,
        )
        check(
            "no identifier or street in the HTTP response",
            "NID-1001" not in json.dumps(body) and "Market Lane" not in json.dumps(body),
        )
        b_ext = ext_of(nodes[1])
        check("second individual: income above threshold -> false", b_ext.get("income") is False, b_ext)

    print("== 2. registration officer, default purpose (registration) without X-Purpose")
    status, body = gql(client, to, q_ind)
    nodes = nodes_of(body, "individual")
    check(
        "officer sees exact values under registration default purpose",
        status == 200
        and nodes
        and nodes[0]["firstName"] == "Amina"
        and nodes[0]["dob"] == "1990-05-04"
        and ext_of(nodes[0]).get("income") == 180,
        body if not nodes else nodes[0],
    )

    print("== 3. case worker under a purpose the role may not use")
    status, body = gql(client, tw, q_ind, **{"X-Purpose": "analytics_reporting"})
    check(
        "analytics_reporting refused for a case worker (ROLE_NOT_PERMITTED)",
        bool(body.get("errors")) and "ROLE_NOT_PERMITTED" in json.dumps(body["errors"]),
        body.get("errors"),
    )

    print("== 4. low-trust device")
    status, body = gql(
        client, tw, q_ind, **{"X-Purpose": "eligibility_verification", "X-Device-Trust": "low"}
    )
    nodes = nodes_of(body, "individual")
    if nodes:
        ext = ext_of(nodes[0])
        check(
            "C4 attributes withheld on a low-trust device, C3 kept",
            "income" not in ext and ext.get("household_size") == 4,
            ext,
        )
    else:
        check("low-trust query returned nodes", False, body)

    print("== 5. beneficiary query with nested individual")
    q_ben = "query { beneficiary(status: ACTIVE) { edges { node { uuid status individual { firstName jsonExt } } } } }"
    status, body = gql(client, tw, q_ben, **{"X-Purpose": "enrollment", "X-Program": "cash_assistance"})
    bnodes = nodes_of(body, "beneficiary")
    check(
        "beneficiary status released under enrollment; nested individual minimised (name denied)",
        status == 200
        and bnodes
        and bnodes[0]["status"] == "ACTIVE"
        and bnodes[0]["individual"]["firstName"] == "***",
        body,
    )

    print("== 6. unmapped operation passes through")
    status, body = gql(client, tw, "query { benefitPlan { edges { node { code name } } } }")
    check(
        "benefitPlan query untouched",
        status == 200 and not body.get("errors") and nodes_of(body, "benefitPlan")[0]["code"] == "CASH",
        body,
    )

    print("== 7. read auditing")
    events = (
        harness.client("audit")
        .get(
            "/v1/events",
            headers=harness.headers(sub="aud-1", role="AUDITOR"),
            params={"event_type": "read_access", "correlation_id": "val-1"},
        )
        .json()["events"]
    )
    check(
        "one read_access event for the Individual entity with the request's correlation id",
        len(events) == 1
        and events[0]["details"]["entity"] == "Individual"
        and events[0]["purpose"] == "eligibility_verification",
        events,
    )
    chain = (
        harness.client("audit")
        .get("/v1/chain/verify", headers=harness.headers(sub="aud-1", role="AUDITOR"))
        .json()
    )
    check("audit chain verifies", chain.get("ok") is True, chain)

    print("== 8. identifier vaulting on createIndividual")
    Individual.objects.filter(first_name="Chidi", last_name="Example").delete()  # idempotent reruns
    configure({"vault_identifiers": True}, control_plane=cp)
    mutation = """
    mutation { createIndividual(input: {firstName: "Chidi", lastName: "Example", dob: "1988-02-02",
      jsonExt: "{\\"national_id\\": \\"NID-3003\\", \\"email\\": \\"chidi@example.test\\", \\"income\\": 240}"}) { clientMutationId } }
    """
    status, body = gql(client, to, mutation, **{"X-Purpose": "registration", "X-Program": "cash_assistance"})
    created = (
        Individual.objects.filter(first_name="Chidi", last_name="Example", is_deleted=False)
        .order_by("-date_created")
        .first()
    )
    stored = created.json_ext if created else None
    check(
        "createIndividual accepted by openIMIS",
        status == 200 and not body.get("errors") and created is not None,
        body,
    )
    if created:
        check(
            "openIMIS stores the placeholder and the person token, never the identifier",
            stored.get("national_id") == "vaulted"
            and str(stored.get("pbd_person_token", "")).startswith("P-")
            and stored.get("income") == 240
            and "NID-3003" not in json.dumps(stored),
            stored,
        )
        again = cp.vault.tokenize(national_id="NID-3003", name="Chidi Example", program="cash_assistance")
        check(
            "vault holds the identity and dedups it",
            again["person_token"] == stored.get("pbd_person_token") and again["created"] is False,
            again,
        )
    configure({"vault_identifiers": False}, control_plane=cp)

    print("== 9. fail closed when the control plane is unreachable")
    configure({}, control_plane=PrivacyControlPlane("http://127.0.0.1:9", token="x", timeout=0.2))
    status, body = gql(client, tw, q_ind, **{"X-Purpose": "eligibility_verification"})
    nodes = nodes_of(body, "individual")
    check(
        "mapped fields error and are null when the control plane is down",
        bool(body.get("errors"))
        and "failing closed" in json.dumps(body["errors"])
        and (not nodes or nodes[0] is None or nodes[0]["firstName"] in (None, "***")),
        body.get("errors"),
    )
    configure({}, control_plane=cp)

    print("== 10. exact cross-program isolation with the Beneficiary-row relationship resolver")
    configure(
        {"relationship_mode": "resolver", "relationship_resolver": "pbd.relationships.from_beneficiaries"},
        control_plane=cp,
    )
    q_amina = (
        'query { individual(firstName_Iexact: "Amina", lastName_Iexact: "Example") '
        "{ edges { node { uuid firstName dob jsonExt } } } }"
    )
    status, body = gql(
        client, tw, q_amina, **{"X-Purpose": "eligibility_verification", "X-Program": "disability_allowance"}
    )
    check(
        "a worker acting for another program is refused (NO_SUBJECT_RELATIONSHIP) for people enrolled only in CASH",
        bool(body.get("errors")) and "NO_SUBJECT_RELATIONSHIP" in json.dumps(body["errors"]),
        body.get("errors"),
    )
    status, body = gql(
        client, tw, q_amina, **{"X-Purpose": "eligibility_verification", "X-Program": "cash_assistance"}
    )
    nodes = [n for n in nodes_of(body, "individual") if n and n["uuid"] == str(amina.uuid)]
    check(
        "the same worker acting for CASH still gets the minimised view",
        status == 200 and not body.get("errors") and len(nodes) == 1 and nodes[0]["firstName"] == "***",
        body,
    )
    configure({}, control_plane=cp)

    print("== 11. migrating identifiers already stored in openIMIS (manage.py pbd_vault_identifiers)")
    from io import StringIO  # noqa: PLC0415

    out = StringIO()
    call_command("pbd_vault_identifiers", "--dry-run", "--username", "pbdofficer", stdout=out)
    check(
        "dry run reports without writing",
        "DRY RUN" in out.getvalue() and "vaulted=" in out.getvalue(),
        out.getvalue(),
    )
    before = Individual.objects.get(id=bo.id).json_ext
    out = StringIO()
    call_command("pbd_vault_identifiers", "--username", "pbdofficer", stdout=out)
    after = Individual.objects.get(id=bo.id).json_ext
    check(
        "existing records rewritten: placeholder + person token, identifier gone",
        before.get("national_id") == "NID-1002"
        and after.get("national_id") == "vaulted"
        and str(after.get("pbd_person_token", "")).startswith("P-")
        and after.get("income") == 900,
        after,
    )
    dedup = cp.vault.tokenize(national_id="NID-1002", name="Bo Example", program="cash_assistance")
    check(
        "migrated identity is in the vault once",
        dedup["person_token"] == after.get("pbd_person_token") and dedup["created"] is False,
        dedup,
    )
    out = StringIO()
    call_command("pbd_vault_identifiers", "--username", "pbdofficer", stdout=out)
    check("rerun is a no-op (all skipped)", "vaulted=0" in out.getvalue(), out.getvalue())

    passed = sum(1 for r in RESULTS if r["ok"])
    print(
        f"\n{passed}/{len(RESULTS)} checks passed against openIMIS core {_version('openimis-be-core')}, "
        f"individual {_version('openimis-be-individual')}, social_protection {_version('openimis-be-social_protection')}, "
        f"Django {django.get_version()}, graphene {__import__('graphene').__version__}"
    )
    (HERE / "last_run.json").write_text(
        json.dumps(
            {
                "results": RESULTS,
                "versions": {
                    "openimis-be-core": _version("openimis-be-core"),
                    "openimis-be-individual": _version("openimis-be-individual"),
                    "openimis-be-social_protection": _version("openimis-be-social_protection"),
                    "django": django.get_version(),
                    "graphene": __import__("graphene").__version__,
                },
            },
            indent=2,
            default=str,
        )
    )
    harness.close()
    return 0 if passed == len(RESULTS) else 1


def _version(dist: str) -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version(dist)
    except PackageNotFoundError:
        return "?"


if __name__ == "__main__":
    raise SystemExit(main())
