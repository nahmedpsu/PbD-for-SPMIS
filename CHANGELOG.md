# Changelog

## 0.2.1 (2026-10-07)

- `openimis-be-pbd` validated inside the real openIMIS backend assembly (core 1.11.0, individual
  1.4.0, social_protection 1.5.0, Django 4.2, graphene 2, PostgreSQL 16): 18/18 checks through
  openIMIS's own GraphQL view. Runner, settings component and recipe in
  `integrations/openimis/validation`.
- Module hardening from that run: redaction marker for denied non-nullable fields, typed
  coercion for `Date`/`DateTime`/numeric fields, graphene 2 promise chaining, middleware ordering
  guidance; `redaction_marker` configuration key.
- Outreach package for the openIMIS community and GovStack in `docs/outreach`.

## 0.2.0 (2026-10-07)

Integration layer for existing MIS products.

- Client SDK (`pbd_spmis.sdk`) for the control plane API v1: decisions with typed errors,
  catalogue view, vault, broker, audit and break-glass clients, shared release transformations,
  and the product-neutral entity/field/role/purpose mapping format.
- `GET /pdp/v1/catalog`: non-sensitive catalogue view for clients.
- `openimis-be-pbd`: openIMIS backend module (graphene middleware) that enforces purpose-bound,
  attribute-level release on Individual/Beneficiary/Group, derives PbD roles from openIMIS right
  codes, audits reads, fails closed, and optionally vaults national identifiers at creation.
- Privacy gateway (`pbd-spmis gateway`): reverse proxy minimising GraphQL and FHIR/REST
  responses for systems that cannot embed the module (legacy CORE-MIS, vendor APIs).
- `date_of_birth` attribute with year precision; list-aware precision transforms.
- Documentation: control plane API v1 and compatibility promise, openIMIS/CORE-MIS integration
  guide, ADR 0006.

## 0.1.0 (2026-10-06)

First public version: a complete, tested reference implementation of the privacy control plane
described in the *Privacy-by-Design Architecture for Social Protection MIS* guide.

- Privacy catalogue (purposes, lawful bases, attribute classification, release modes, role
  overrides, programs, context policy, retention, sharing matrix) with JSON Schema and
  cross-reference validation.
- Policy Decision Point with an embedded Python engine and an OPA/Rego policy; 31 hand-written
  conformance vectors replayed against both engines and the HTTP contract.
- Policy Enforcement Point library with release transformations and obligation headers.
- Identity Vault with envelope encryption, blind-index deduplication, program-specific
  identifiers and policy-gated resolution.
- Social Registry, Program Store (enrollment, entitlement, cases), Eligibility (Appendix B
  contract), Payments (tokenised instruments, provider adapter, reconciliation).
- Data exchange broker with sharing matrix, schema filtering, just-in-time identifier resolution
  and TTL cache; mock tax, civil and disability sources.
- Break-glass manager with MFA, separation of duties, scoped time-bound grants, alerts and review.
- Hash-chained, PII-guarded audit store with self-logging reads and the privacy dashboard.
- Retention engine executing archive / anonymise / delete through owning services.
- Generated OpenAPI contracts, Docker image, Compose (PostgreSQL + OPA, distributed profile),
  GitHub Actions CI, documentation set and ADRs.
