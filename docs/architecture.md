# Architecture

This document describes how the repository realises the logical architecture of the guide:
access channels, a privacy-aware access layer, core services, a cross-cutting privacy control
plane, separated data stores, an integration layer and a de-identified analytics path.

## Services

| Service | Role in the guide | Owns | Talks to |
| --- | --- | --- | --- |
| `pdp` | Policy Decision Point (4.3) | nothing persistent | audit |
| `audit` | Privacy audit / transparency service; Audit/Decision Store (5.2) | hash-chained event log | – |
| `vault` | Identity Vault + tokenization/pseudonymization service (5.1) | encrypted identities, program links | pdp, audit, registry (relationships), retention |
| `registry` | Social Registry (5.2) | households, socioeconomic attributes, program relationships | pdp, audit, retention |
| `program` | Program Store: enrollment, entitlement, cases (5.2, 6.4) | enrollments, cases | pdp, audit, registry, eligibility, vault, retention |
| `broker` | Privacy-preserving data exchange broker (6.2, 8.2) | TTL assertion cache | pdp, audit, registry, vault, external adapters |
| `eligibility` | Eligibility service (6.2, Appendix B) | determinations with evidence | pdp, audit, registry, broker, retention |
| `breakglass` | Break-glass manager (4.3, 12.1) | grants | audit |
| `payments` | Payment Reference Store + payment adapter (5.2, 6.3) | encrypted instruments, instructions | pdp, audit, registry, vault, program, retention |
| `retention` | Retention/deletion engine (4.3) | schedules | audit, every owning service |

Each service is a FastAPI application with its own SQLAlchemy `Base`, engine and database. In the
all-in-one mode they are mounted under `/<service>` and call each other in-process through an
httpx transport that drives the target ASGI app; in distributed mode the same client uses
`PBD_<SERVICE>_URL`. Every downstream call is made with the *calling service's* machine identity
(a distinct `*_SERVICE` role), never by forwarding the end user's token; the originating purpose,
program, correlation id and context travel in headers.

## Request context and the privacy headers

Every sensitive endpoint depends on `request_context`, which authenticates the bearer token and
reads:

| Header | Meaning |
| --- | --- |
| `X-Purpose` | registered purpose, required |
| `X-Program` | program the request is for, required |
| `X-Correlation-ID` | propagated to downstream calls and audit events |
| `X-Channel` | api, portal, assisted, mobile |
| `X-Device-Trust` | low, managed, high (asserted by the access layer) |
| `X-Case-ID` | case the actor is working; required by case-scoped purposes |
| `X-Break-Glass-Grant` | grant id for the `emergency_protection` purpose |

Responses from sensitive endpoints carry `X-Decision-ID`, `X-Policy-Version` and
`X-Obligations`; a `no_export` obligation also sets `Cache-Control: no-store`.

## The Policy Enforcement Point

`common/pep.py` is embedded in every service. `enforce()` builds the policy input (actor from
the token, subject token and its program relationships, program, purpose, action, attribute
names, context), asks the PDP, records an `access_decision` audit event and raises a 403 with the
reason codes on deny. `apply_release()` turns the raw record into what the decision permits:

| Mode | Transformation (performed by the data-holding service) |
| --- | --- |
| `exact` | value as stored |
| `band` | value mapped to the attribute's configured bands (`income` → `100-249`) |
| `assertion:below_threshold` | `value < program.income_threshold` |
| `assertion:eligible` | `value in program.eligible_disability_statuses` |
| `precision:district` / `precision:region` | one component of a structured value |
| `token` | opaque reference instead of the value |
| `verify_only` | `{"verified": true/false}` |
| `masked` | last two characters |
| `deny` | omitted |

Services never see a decision they could misread: if the PDP is unreachable the PEP raises
`upstream_unavailable` and the read fails closed.

## Identifiers and stores

```
Identity Vault        person_token  ──┐   national_id, name, contact: AES-GCM under a per-record DEK,
                                      │   DEK wrapped by the KeyProvider; national_id blind index (HMAC)
Social Registry       person_token  ──┤   household_token, income, household_size, disability_status,
                                      │   address, employment_status, vulnerability, program_relationships
Program Store         program_person_id ─ enrollment, entitlement, eligibility outcome, cases
Payment Reference     payment_token ───── encrypted instrument; instructions by program_person_id
Eligibility           determination_id ── outcome + evidence (assertion, source, verified_at)
Broker cache          person_token ────── filtered assertion, expires_at
Audit                 event_id / seq ──── tokens and attribute *names* only; hash chain
```

Only the vault maps `program_person_id` to `person_token` (service identities only, audited)
and only the vault can release identifiers (policy decision plus a `token_resolved` audit event
that fails closed if the audit store is down).

## Flow: eligibility verification ("query, do not copy")

```mermaid
sequenceDiagram
  participant CW as Case worker
  participant EL as Eligibility
  participant PDP
  participant BR as Broker
  participant V as Vault
  participant TAX as Tax authority
  participant AU as Audit
  CW->>EL: POST /v1/eligibility/verify {person_token} + X-Purpose: eligibility_verification
  EL->>PDP: decide(attributes: income, national_id, …)
  PDP-->>EL: income: assertion:below_threshold, national_id: verify_only
  EL->>BR: POST /v1/verify {checks: identity_status, income_threshold}
  BR->>PDP: decide per check (sharing matrix + purpose)
  BR->>V: POST /v1/resolve {national_id} under external_verification
  V->>AU: token_resolved (must record)
  V-->>BR: national_id (in memory only)
  BR->>TAX: income_threshold(national_id, threshold)
  TAX-->>BR: {met: true}  (schema-filtered)
  BR->>AU: external_disclosure {source, check, keys_returned}
  BR-->>EL: {income_threshold: {met: true, source, verified_at}}
  EL->>EL: evaluate program rules → eligible; store determination + evidence
  EL-->>CW: Appendix B response: result, outcome, determination_id, decision_id
```

## Flow: payment

The program store holds entitlements keyed by `program_person_id`. The payment service looks the
program id up in the vault (service-only link lookup), creates an instruction referencing the
payment *token*, and at execution the provider adapter decrypts the instrument inside the
payments service and resolves the beneficiary name just in time under `token_resolution`. Case
workers and finance officers read status, amount and exception reason only; the finance officer
role override denies the instrument even under the payment purpose.

## Flow: break-glass

1. A case worker with MFA requests a grant naming the subject, attributes, reason and duration.
2. A supervisor (not the requester) with MFA approves; the grant becomes active with an expiry
   and a `break_glass_activated` audit event with an alert is written (must record).
3. The worker sends `X-Break-Glass-Grant` with the `emergency_protection` purpose. The PEP loads
   the grant and passes `{active, actor_id, matches_subject, attributes}` to the PDP, which
   releases exactly the granted attributes with `alert` and `review_required` obligations.
4. Expiry or revocation deactivates the grant; an independent reviewer records the outcome; the
   dashboard lists grants overdue for review.

## Analytics path

`GET /registry/v1/export` is only reachable through a purpose that permits the `export` action
(`analytics_reporting`). Rows are produced through the same release transformations (bands,
regions), and the `k_anonymity:N` obligation is enforced by suppressing any combination of
released values that occurs fewer than N times. Exports are audited with row counts.

## Deployment shapes

- **All-in-one** (`pbd-spmis serve all`): development, demos, tests; SQLite per service.
- **Compose** (`docker compose up`): PostgreSQL with one database per service, OPA serving the
  Rego policy and the catalogue bundle, PDP in `opa` mode.
- **Distributed** (`--profile distributed`): one container per service wired by `PBD_*_URL`.

See [deployment.md](deployment.md) for the production hardening checklist mapped to the guide's
security zones and control baseline.
