# From the guide to the code

A section-by-section trace from the *Privacy-by-Design Architecture for Social Protection MIS*
guide to where each requirement is implemented and tested.

| Guide section | Requirement | Implementation | Verified by |
| --- | --- | --- | --- |
| 1.2 Minimum necessary disclosure | release only the attribute, assertion or precision required | release modes in `catalog/attributes.yaml`; `pep.apply_release` | `appendix_a_*` vectors; `test_minimisation_*` |
| 1.2 Purpose-bound access | every request carries actor, program, purpose, lawful basis, attributes | `common/context.py` headers; `purposes.yaml` lawful bases | `test_requests_without_purpose_or_token_are_rejected` |
| 1.2 Identity/data separation | identifiers isolated with token linkage | `vault` + opaque ids in `common/ids.py` | `test_identity_proofing_*`, `test_token_unlinkability` |
| 1.2 Interoperability without replication | verified queries over bulk copies | `broker` + `sharing.yaml` | `test_eligibility_queries_do_not_copy_*` |
| 1.2 Auditable decisions | log decisions, releases, exceptional access, admin changes | `audit` service; `audit_client.emit` everywhere | `test_pdp_decisions_are_audited`, audit tests |
| 1.2 Security by default | strong auth, encryption, least privilege, secrets | HS256 dev tokens with OIDC seam; AES-GCM envelope encryption; service identities per service | `test_primitives.py` |
| 2 Purpose limitation | process only for registered purposes | gate `UNKNOWN_PURPOSE`, `PURPOSE_NOT_ALLOWED_FOR_PROGRAM` | vectors |
| 2 Data minimisation | forms/APIs/reports expose only needed fields | per-purpose release maps; `attributes=` query parameter | vectors; `test_export_*` |
| 2 Disassociability | program-specific identifiers | `vault` program links; `program` store keyed by them | `test_enrollment_payment_*` |
| 2 Manageability | admins change purposes/retention/sharing without rewriting apps | catalogue YAML + schema + `pbd-spmis catalog validate` | `test_catalog.py` |
| 2 Retention discipline | retention per data class and purpose | `retention.yaml`; `retention` engine; `/v1/internal/retention` in every store | `test_retention_engine_executes_end_actions` |
| 2 Consent is not the universal basis | statutory authority, mandate, obligation modelled | `lawful_bases` in `purposes.yaml` | schema validation |
| 3 Privacy control plane as cross-cutting layer | – | `pdp`, `vault`, `broker`, `breakglass`, `retention`, `audit` + PEP in each core service | architecture.md |
| 4.2 RBAC + ABAC/PBAC | roles coarse, attributes/purpose fine | gates 6-8 + per-attribute release | vectors |
| 4.2 Service identities | distinct machine identity per service | `clients.service_token` + `SERVICE_ROLES` | `service_role_claim_without_service_flag` |
| 4.2 Session risk | re-authenticate / restrict on risky sessions | `X-Device-Trust`; `low_trust_denies_classes` | `low_trust_device_*` vectors; `test_low_trust_device_*` |
| 4.3 Purpose registry, PDP, PEPs, minimisation, selective disclosure, tokenisation, consent/notice, retention, break-glass, audit, sharing policy | – | catalogue; `pdp`; `pep.py`; `vault`; `retention`; `breakglass`; `audit`; `sharing.yaml` (notice service: roadmap) | – |
| 5.1 Separate identity from social and case data | – | vault vs registry vs program store | `test_identity_proofing_*` |
| 5.2 Stores and protection expectations | vault highest classification, append-only audit, analytics without identifiers | envelope encryption; hash chain; `/v1/export` transformations | audit and export tests |
| 5.3 Data classification C1–C4 | – | `attributes.yaml` `class` | catalogue tests |
| 6.1 Registration | capture minimum, notice version, token, vault/registry split, retention metadata | `vault` identities → `registry` persons; retention schedule hook (notice versioning: roadmap) | harness `register_person` |
| 6.2 Eligibility verification | request only needed evidence; store result and provenance | `eligibility` + `broker` | `test_eligibility_*` |
| 6.3 Payment workflow | tokens, adapter resolves only what the provider needs, workers see status | `payments` + `provider.py` | `test_enrollment_payment_and_status_views` |
| 6.4 Case management | sensitivity labels, relationship-based access | `program` cases; `requires_case_assignment` | `test_case_management_relationship_based_access` |
| 7 Policy enforcement model | input/decision shapes, decision sequence, fail-closed | `pdp/engine.py`, `authz.rego`; `PBD_FAIL_CLOSED` | conformance; `test_sensitive_reads_fail_closed_*` |
| 8.1 API principles | contract-first, opaque ids in URLs, purpose/program context, versioned policy bundles | generated OpenAPI contracts; token ids in paths; `policy_version` | `test_contracts.py` |
| 8.2 Query, do not copy patterns | income threshold, identity status, disability eligibility, residence, payment token | `sharing.yaml` checks; adapters | eligibility tests |
| 8.3 Event model | events with opaque identifiers | audit event types (`eligibility_calculated`, `enrollment_approved`, `payment_issued`, `retention_expired`, …) | audit tests |
| 9.2 MVP sequence | IAM+gateway, vault+tokens, purpose registry+PDP, registry with tokens, one eligibility integration, audit dashboards, one payment provider | all present in this version | demo |
| 10 Technology | OPA as policy-as-code option, PostgreSQL, KMS | `PBD_POLICY_ENGINE=opa`; compose with PostgreSQL; `KeyProvider` seam | compose |
| 11 Deployment zones | – | `docker-compose.yml` profiles; zone mapping in deployment.md | CI image smoke test |
| 12 Control baseline | default deny, encryption, no secrets in code, tamper-evident audit, export control | gates; AES-GCM; env/KMS; hash chain; export action | tests |
| 12.1 Break-glass | reason, stronger auth, scope, duration, alert, review | `breakglass` service + PDP gates 12-15 | `test_break_glass_*`; six vectors |
| 13 NFRs | audit completeness, traceability via correlation ids without PII | correlation ids on every event; PII guard | audit tests |
| 14.1 Privacy tests | purpose, minimisation, isolation, unlinkability, export, retention, break-glass, audit integrity | conformance vectors + end-to-end tests | README table |
| 15 Migration | identities cleansed, tokens issued, legacy fields excluded | blind-index dedup at proofing; token issuance | `test_identity_proofing_*` |
| 16.1 Operational dashboards | denied access, break-glass, exports, sharing by purpose, retention expiry, token resolutions | `GET /audit/v1/metrics`; `GET /breakglass/v1/grants?status=overdue_review`; `GET /retention/v1/schedules?due_within_days=` | tests |
| Appendix A | example policy decision | first conformance vector | `test_appendix_a_example` (Rego) |
| Appendix B | eligibility API contract | `POST /eligibility/v1/eligibility/verify` | `test_appendix_b_contract_shape` |
| Appendix C | classification and retention template | `attributes.yaml` + `retention.yaml` | catalogue tests |
