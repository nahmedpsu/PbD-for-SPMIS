# Validating `openimis-be-pbd` inside the real openIMIS backend

`run_validation.py` boots the genuine openIMIS backend assembly (`openimis-be_py`) with the
published openIMIS modules, migrates PostgreSQL, seeds roles, users, a benefit plan and
individuals, starts the PbD control plane in-process, and issues real GraphQL requests through
openIMIS's own view with JWTs issued by openIMIS. It is the integration test for the module; the
unit tests in `tests/test_openimis_module.py` need no openIMIS.

## Last result

```
24/24 checks passed against openIMIS core 1.11.0, individual 1.4.0, social_protection 1.5.0,
Django 4.2.30, graphene 2.1.9 (PostgreSQL 16)
```

The exact outcome of the last local run is in `last_run.json`.

## What it checks

| # | Check |
| --- | --- |
| 0 | the seeded openIMIS users carry the reference right codes (159001, 170001, ...) |
| 1 | case worker + `X-Purpose: eligibility_verification`: names redacted, `dob` reduced to the year, `jsonExt` minimised key by key (`income: true`, `national_id: {verified: true}`, address → district, unmapped keys kept), no identifier or street anywhere in the response |
| 2 | registration officer without `X-Purpose`: the mapping's default purpose (`registration`) releases exact values |
| 3 | case worker under `analytics_reporting`: refused with `ROLE_NOT_PERMITTED` |
| 4 | `X-Device-Trust: low`: C4 attributes withheld, C3 kept |
| 5 | `beneficiary` query under `enrollment`: status released, nested individual minimised |
| 6 | unmapped operation (`benefitPlan`) passes through untouched |
| 7 | one `read_access` audit event per entity per request with the request's correlation id; audit chain verifies |
| 8 | `createIndividual` with vaulting on: openIMIS stores the placeholder and the person token, never the identifier; the vault deduplicates it |
| 9 | control plane unreachable: mapped fields error and are redacted (fail closed) |
| 10 | `relationship_mode: resolver`: a worker acting for `disability_allowance` is refused (`NO_SUBJECT_RELATIONSHIP`) for people enrolled only in the CASH plan, and still served for `cash_assistance` |
| 11 | `manage.py pbd_vault_identifiers`: dry run, migration of existing records (placeholder + token, identifier gone), vault deduplication, idempotent rerun |

## Prerequisites

- Python 3.12 (openIMIS pins Django 4.2 and graphene 2).
- PostgreSQL 16 with the `postgres-json-schema` extension installed
  (`git clone https://github.com/gavinwahl/postgres-json-schema && make install`, then
  `CREATE EXTENSION "postgres-json-schema"` in the database).
- The openIMIS database scripts applied to an empty database, as the openIMIS database image
  does: `00_dump.sql`, `02_aux_functions.sql`, `03_views.sql`, `04_functions.sql`,
  `05_stored_procs.sql` from https://github.com/openimis/database_postgresql
  (`database scripts/`). openIMIS's legacy tables (`tblUsers`, `tblRole`, ...) come from these
  scripts, not from Django migrations.
- A checkout of https://github.com/openimis/openimis-be_py (the assembly) and its
  `requirements.txt` installed (without `mssql-django`/`pyodbc`), plus `setuptools<81`
  (`apscheduler` still imports `pkg_resources`) and `sentry-sdk`.
- The openIMIS modules from PyPI: `openimis-be-core==1.11.0`, `openimis-be-individual==1.4.0`,
  `openimis-be-social-protection==1.5.0`, `openimis-be-calculation`, `openimis-be-workflow`,
  `openimis-be-tasks-management`, `openimis-be-location`, and the migration dependencies of
  `calculation` (`contribution_plan`, `product`, `medical`, `medical_pricelist`). `openimis.json`
  in this directory lists the exact set.
- This repository and the module installed into the same environment:
  `pip install -e . && pip install --no-deps -e integrations/openimis/openimis-be-pbd_py`.

## Running

```bash
export OPENIMIS_BE_DIR=/path/to/openimis-be_py
export DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=imis DB_USER=imis DB_PASSWORD=imis
python integrations/openimis/validation/run_validation.py
```

The runner installs `validation_settings.py` as `openIMIS/settings/pbdvalidation.py` in the
assembly checkout and starts it with `MODE=pbdvalidation`, which is the assembly's own mechanism
for adding a settings component (and therefore the way a deployment adds the middleware).

## What running inside real openIMIS taught the module

These behaviours were discovered here and are now part of the module (and its unit tests):

1. **Non-nullable fields.** openIMIS declares `firstName`, `lastName` and `jsonExt` as
   `String!`/`JSONString!`. A denied attribute cannot be returned as `null` without nulling the
   whole node, so the module returns a configurable redaction marker (`***`) for denied non-null
   strings and errors for other non-null types.
2. **Typed fields.** `dob` is a GraphQL `Date`; year precision is returned as the first day of
   the year (a date value), not the string `"1990"`.
3. **Promises.** openIMIS runs graphene 2 / graphql-core 2, whose executor hands middleware
   `Promise` objects; the transform is chained onto the promise.
4. **Middleware order.** openIMIS's `TracerMiddleware` wraps exceptions and, without a tracing
   backend, fails on `FakeSpan.log_kv`; the privacy middleware is therefore placed *last* in
   `GRAPHENE["MIDDLEWARE"]` so its policy errors reach the client intact.
5. **CSRF in the session.** openIMIS resolvers compare `X-CSRFToken` with a token stored in the
   Django session at login even on JWT requests; API clients must log in through `tokenAuth` or
   carry the session, which the runner seeds directly.
