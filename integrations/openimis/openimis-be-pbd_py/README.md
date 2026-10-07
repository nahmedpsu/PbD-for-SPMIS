# openimis-be-pbd

Privacy-by-Design enforcement module for the openIMIS backend (and therefore for CORE-MIS
powered by openIMIS). It connects an openIMIS assembly to the [PbD-SPMIS control
plane](../../../README.md): purpose-bound, attribute-level release decisions, read auditing, and
optional identifier vaulting, with no change to the existing openIMIS modules.

## What it adds to openIMIS

| openIMIS today | With this module |
| --- | --- |
| Role + geographic rights decide whether a user may run a query | The same rights, mapped to a PbD role, plus the **purpose** of the request decide **which attributes** are released and **in which form** (exact, band, assertion, district, masked, denied) |
| `firstName`, `lastName`, `dob`, `jsonExt.national_id` returned to any permitted user | A case worker verifying eligibility receives the year of birth and `income: true` (below threshold); the national id is never returned; a finance officer sees a masked name |
| Mutation log records writes | Every read of a mapped entity produces a `read_access` event in a hash-chained audit store, in addition to the control plane's decision log |
| National identifiers stored in `individual.json_ext` | Opt-in: identifiers go to the Identity Vault at `createIndividual`; openIMIS keeps a person token |
| No fail-closed behaviour on authorisation dependencies | If the control plane is unreachable, mapped fields error instead of leaking |

## Install

1. Run the control plane (`pbd-spmis serve all` or the Compose stack) and issue a service token:
   `pbd-spmis token --sub svc:openimis --role REGISTRY_SERVICE --programs '*' --service`.
2. Add the module to `openimis.json` of the backend assembly:

   ```json
   { "name": "pbd", "pip": "openimis-be-pbd==0.1.0" }
   ```

3. Add the middleware to the graphene settings of `openimis-be_py`:

   ```python
   GRAPHENE["MIDDLEWARE"] = [*GRAPHENE["MIDDLEWARE"], "pbd.middleware.PrivacyMiddleware"]
   ```

   The privacy middleware goes **last** so that its policy errors reach the client intact
   (openIMIS's tracer middleware re-wraps exceptions). The simplest way is a settings component
   selected by `MODE`, as `integrations/openimis/validation/validation_settings.py` shows.

4. Configure the module (ModuleConfiguration for `pbd`, or environment variables):

   | Key | Default | Meaning |
   | --- | --- | --- |
   | `control_plane_url` | `http://localhost:8000` | all-in-one base URL (or `service_urls` per service) |
   | `service_token` | – | bearer token for this openIMIS instance |
   | `mapping_file` | bundled `mapping.yaml` | entity/field/role/purpose mapping |
   | `require_purpose_header` | `false` | deny mapped reads without `X-Purpose` instead of using the operation default |
   | `relationship_mode` | `assume` | `assume` (subject related to the request's program) or `resolver` (exact: `relationship_resolver: pbd.relationships.from_beneficiaries` reads `Beneficiary`/`GroupBeneficiary` rows) |
   | `fail_closed` | `true` | error on control-plane unavailability |
   | `audit_reads` | `true` | emit `read_access` events |
   | `vault_identifiers` | `false` | move identifiers to the vault on create/update |
   | `redaction_marker` | `***` | value returned for denied attributes whose GraphQL field is non-nullable (`firstName: String!`) |

5. Have the frontend (or API clients) send `X-Purpose` and, where relevant, `X-Program`,
   `X-Case-ID` and `X-Device-Trust`. Without `X-Purpose` the mapping's per-operation default is
   used, so existing clients keep working while they are migrated.

## Migrating identifiers already stored in openIMIS

```bash
manage.py pbd_vault_identifiers --dry-run --username Admin
manage.py pbd_vault_identifiers --program cash_assistance --batch 500 --username Admin
```

`--username` names the existing openIMIS user the audited saves are attributed to.

Every `Individual` whose `json_ext` carries an identifier is proofed in the Identity Vault under
the `identity_proofing` purpose; openIMIS keeps the placeholder and the person token. Rows already
vaulted are skipped, so the command is safe to rerun, and the vault deduplicates identifiers.

## Mapping

`integrations/openimis/mapping.yaml` declares, per deployment:

- which GraphQL types and fields are which catalogue attributes (`IndividualGQLType.firstName`
  → `name`, `jsonExt.national_id` → `national_id`, ...);
- which openIMIS right codes map to which PbD role (ordered rules; first match wins);
- the default purpose and action for each GraphQL operation;
- benefit plan codes to catalogue programs;
- the vaulting rules.

## Validated against real openIMIS

`integrations/openimis/validation/run_validation.py` runs the module inside the genuine
`openimis-be_py` assembly with openIMIS core 1.11.0, individual 1.4.0 and social_protection 1.5.0
on PostgreSQL 16 (Django 4.2, graphene 2): 24 of 24 checks pass, covering minimised views per
purpose through openIMIS's own GraphQL view and JWTs, default purposes, role derivation from
right codes, pass-through, low-trust downgrades, read auditing, identifier vaulting and
fail-closed behaviour. See the validation README for the recipe and for what real openIMIS
taught the module (non-null fields, typed fields, graphene 2 promises, middleware order).

## Limitations in this version

- Subject relationships default to "the subject is related to the request's program"; switch to
  `relationship_mode: resolver` (bundled `pbd.relationships.from_beneficiaries`) for exact
  cross-program isolation at the cost of one query per subject and request.
- Decisions are taken per entity per request (not per field), which keeps the control-plane
  round trips to one per entity type per request.
- Vaulting rewrites `createIndividual`/`updateIndividual`; records created by bulk imports are
  caught by the `pbd_vault_identifiers` command rather than at import time.
