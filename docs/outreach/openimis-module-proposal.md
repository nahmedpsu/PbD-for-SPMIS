# Proposal to the openIMIS community: `openimis-be-pbd`, a Privacy-by-Design enforcement module

**Submitted by:** nahmedpsu · **Repository:** https://github.com/nahmedpsu/PbD-for-SPMIS ·
**License:** AGPL-3.0 (module), Apache-2.0 (control plane)

## Summary

`openimis-be-pbd` is a backend module that adds purpose-bound, attribute-level data
minimisation, read auditing, fail-closed authorisation and optional identifier vaulting to any
openIMIS assembly, without changing existing modules. It implements the privacy requirements
the openIMIS wiki already lists as open ("mark personal data", "audit log of access to personal
data", data-subject access to their own logs) and goes further on minimisation. It is backed by
an open, tested control plane with a 31-vector conformance suite that both a Python evaluator
and an OPA/Rego policy pass.

## The problem it addresses

openIMIS authorises with roles made of numeric right codes, scoped geographically. A user who
may search beneficiaries sees every field of every beneficiary in scope, for every reason. As
openIMIS carries more social protection programs (cash transfers, economic inclusion,
disability allowances), the profile it holds per person becomes richer and the legal exposure
of this model grows: data protection laws in most implementing countries require purpose
limitation and minimisation, not only authentication and role checks.

## What the module does

1. **Purpose binding.** Clients send `X-Purpose` (and optionally `X-Program`, `X-Case-ID`,
   `X-Device-Trust`). Until frontend modules are updated, a mapping gives each GraphQL
   operation a default purpose, so nothing breaks on day one.
2. **Attribute-level release.** For each mapped type (`IndividualGQLType`,
   `BeneficiaryGQLType`, `GroupGQLType`) the module asks the control plane which attributes
   the purpose permits and in which form: exact, band, assertion (`income: true` meaning
   below the program threshold), reduced precision (`dob: "1990"`, address → district),
   masked, or denied. `jsonExt` is handled key by key against the deployment's JSON schema.
3. **Role derivation.** The deployment's right codes map to PbD roles through an ordered rule
   list in `mapping.yaml`; no changes to openIMIS roles are needed.
4. **Read auditing.** One hash-chained `read_access` event per entity per request, in addition
   to openIMIS's mutation log, satisfying "audit log of access to personal data".
5. **Fail closed.** If the control plane is unreachable, mapped fields error instead of leaking.
6. **Identifier vaulting (opt-in).** `createIndividual`/`updateIndividual` send the national
   identifier to an Identity Vault and store a person token; openIMIS then never holds the
   identifier, which removes it from every backup, export and breach scenario.

Integration surface used: graphene middleware, `AppConfig.ready`, `ModuleConfiguration`,
`bind_service_signal`. No model changes, no migrations.

## What we ask of the community

- Review of the mapping for the reference social protection modules (right codes, `jsonExt`
  conventions, benefit plan codes).
- A slot at a developers committee meeting to demonstrate the module on the reference Docker
  distribution.
- Agreement in principle to list the module as an optional module of the reference assembly
  (`openimis.json`), with the frontend modules progressively sending `X-Purpose`.
- Guidance on where the control plane should run in the reference deployment (a sidecar
  container next to the backend is the current recommendation).

## What we offer

- Maintenance of the module against openIMIS releases, with tests that run without a database
  (graphene schema shaped like openIMIS's) and an integration test against the real modules.
- The conformance vectors as acceptance criteria any deployment can run.
- Documentation mapping each control to the openIMIS data privacy requirements page.

## Evidence

- `integrations/openimis/openimis-be-pbd_py` (module), `integrations/openimis/mapping.yaml`.
- `tests/test_openimis_module.py`: minimised views per purpose, role derivation, pass-through,
  low-trust downgrades, read auditing, vaulting, fail-closed.
- `docs/integrations/openimis.md`: deployment patterns and limitations.
- `docs/decision-model.md` and `policy/conformance/vectors.json`: the authorisation model.

## Known limitations

- Subject relationships default to the request's program; a resolver reading `Beneficiary`
  rows gives exact cross-program isolation and is the first follow-up.
- Vaulting covers the GraphQL mutations; bulk imports are audited but not yet vaulted.
- Field-level decisions are taken per entity per request, which is deliberate for performance.
