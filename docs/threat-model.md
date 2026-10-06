# Threat model

Scope: the privacy control plane and reference services in this repository, deployed in the
distributed shape. Assets, in order of impact: direct identifiers and their link to program
data; health, protection and financial attributes; the audit log's integrity; the policy itself.

| Threat | Mitigation in this implementation | Residual / production note |
| --- | --- | --- |
| Over-privileged user browses beneficiary profiles | Purpose-bound, attribute-level decisions; subject relationship and case assignment gates; task-based endpoints only (no list/search of persons) | Search endpoints added later must carry the same gates |
| Insider exports a bulk extract | `export` is an action only analytics purposes permit; rows pass release transformations; k-anonymity suppression; export audited with counts | Add rate limits and DLP on the analytics zone |
| Program database breach | Program stores are keyed by program-specific identifiers; no identifiers, no joins without the vault | Protect the vault's link table like the identities themselves |
| Vault database breach | Fields encrypted under per-record DEKs wrapped by the KMS; blind index is keyed HMAC | Use an HSM-backed KeyProvider; the dev provider is for laptops only |
| Identifier leaks through inter-agency calls | Broker resolves identifiers just in time under a `no_persist` obligation; response schema filter; sharing matrix fixes who may ask what | Real adapters must not log request bodies |
| Identifier leaks through logs | Audit PII guard rejects identifier keys and narrative text; audit events carry tokens and attribute names only | Application logs are the operator's responsibility (see deployment.md) |
| Policy engine down → services "fail open" | PEP raises `upstream_unavailable`; sensitive reads refused | Any continuity mode must be explicit, narrow and audited (guide 7) |
| Audit store down → silent disclosure | Disclosures, token resolutions, break-glass activations and exports fail closed if the audit write fails | Other events are best-effort; monitor audit write failures |
| Audit log tampering | SHA-256 hash chain; `chain/verify` detects edits, deletions and reordering | Anchor the chain head externally (e.g. daily to a notary or WORM store) |
| Audit log read by the curious | Reads restricted to auditor roles and services; every human read is itself logged | – |
| Break-glass abuse | MFA to request and approve; approver ≠ requester; grants scoped to one subject and named attributes; expiry ≤ 4h; alert on activation; independent review; PDP checks holder, subject and activity | Review SLA enforcement is a dashboard item, not a technical block |
| Token theft / replay | Short service token TTL; purpose and program bound per request; device-trust context | Production: OIDC with sender-constrained tokens or mTLS |
| Wildcard program assignment claimed by a human | `*` honoured only for `is_service` tokens with a service role | IdP must not let users self-assert `svc` |
| Catalogue misconfiguration | JSON Schema + cross-reference validation (unknown attributes, modes not permitted for an attribute, C4 exact release on export purposes, overrides for roles not allowed); conformance vectors | Changes to the catalogue need review by the privacy function |
| Divergence between policy engines | Same vectors replayed against the Python engine, the Rego policy and the HTTP contract in CI | – |
| Re-identification from analytics | Bands, region-level location, assertions; k-anonymity | Add l-diversity / differential privacy for published statistics |
| Cross-column ciphertext swapping in the vault | Field name bound as AEAD associated data | – |

Not in scope for this version: denial of service, supply-chain integrity of the container
image, browser-side threats in a citizen portal, and physical security of HSMs.
