# Proposal to GovStack: a Purpose-bound Data Minimisation capability

**Submitted by:** nahmedpsu · **Reference implementation:** https://github.com/nahmedpsu/PbD-for-SPMIS

## Why GovStack

GovStack specifies reusable building blocks for digital government: Identity, Registration,
Consent, Payments, Information Mediator and others. The Consent building block records what a
person has agreed to, and the Information Mediator secures data exchange between systems. What
no building block currently specifies is the control between the two: given an authenticated
actor, a registered purpose and a request for personal attributes, *which attributes may be
released, in what form, under which obligations, and how is that decision evidenced*. Every
sectoral system (social protection, health, education) re-implements this gap, usually as
role-based access with no purpose concept and no minimisation.

## The proposal

Specify a **Purpose-bound Data Minimisation** capability, either as a new building block or as
a mandatory interface of the Information Mediator and Consent building blocks, consisting of:

1. **A policy decision interface.** Input: actor (role, assignments, authentication strength),
   subject reference (opaque token and program relationships), program, purpose, action,
   requested attribute names, context (channel, device trust, case, exceptional-access grant).
   Output: allow/deny with stable reason codes, a per-attribute release mode from a closed
   vocabulary (`exact`, `band`, `assertion:<name>`, `precision:<level>`, `token`,
   `verify_only`, `masked`, `deny`), obligations, and the policy version.
2. **A catalogue format** for purposes with lawful bases, attribute classification, per-purpose
   release maps with role overrides, programs, retention and sharing rules, so that rules are
   administered as reviewed data rather than code.
3. **Obligations as part of the contract** (`log`, `no_export`, `no_persist`,
   `expire_response:<ttl>`, `k_anonymity:<k>`, `alert`, `review_required`), carried to the
   consumer as response metadata.
4. **An audit event schema** that records that attributes were released (names, modes,
   decision id, policy version, correlation id) and never the values, in a tamper-evident log.
5. **A conformance suite**: executable vectors that any implementation must satisfy, covering
   purpose enforcement, minimisation, cross-program isolation, token unlinkability, export
   control, exceptional access and context downgrades.

## Relationship to existing building blocks

| Building block | Interaction |
| --- | --- |
| Consent | consent is one lawful basis in the catalogue; the decision interface consults it when a purpose requires it |
| Identity | identity proofing produces the opaque subject token; the vault pattern keeps identifiers out of sectoral systems |
| Registration | registrations declare purposes and minimum datasets from the catalogue |
| Information Mediator | the "query, do not copy" broker pattern (assertions over records, schema-filtered responses, TTL caches) is the data-exchange profile of this capability |
| Payments | payment execution receives tokens and names, never account data outside the payment system |

## What exists today

The reference implementation provides all five components with tests: a policy decision point
with two engines (Python and OPA/Rego) passing the same 31 vectors, a validated catalogue, a
hash-chained audit store, an exchange broker, and adapters for openIMIS (the most widely used
open-source social protection MIS) and for generic GraphQL/FHIR APIs. It was derived from a
published architecture guide for social protection MIS and generalises to other sectors.

## What we ask

- A review slot with the technical committee and the Consent and Information Mediator working
  groups.
- Feedback on whether to pursue a standalone building block or interfaces within existing ones.
- Permission to contribute the decision interface, catalogue schema and conformance vectors as
  a draft specification in the GovStack specification repositories, with the reference
  implementation as a candidate compliant product.

## Contacts and material

- Decision model: `docs/decision-model.md`; API: `docs/control-plane-api.md`;
  conformance: `docs/conformance.md`; threat model: `docs/threat-model.md`.
- OpenAPI contracts: `contracts/openapi/`; Rego policy: `policy/rego/`.
