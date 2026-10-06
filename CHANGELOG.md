# Changelog

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
