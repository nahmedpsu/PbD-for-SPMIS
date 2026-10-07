# Novelty and contributions: material for a manuscript

This note states what is new in PbD-SPMIS, what is sound engineering of known ideas, how to
frame the work for peer review, and what evidence the repository already provides. It is written
to be lifted into a manuscript's introduction, contributions list, evaluation and limitations.

## 1. One-sentence thesis

> Privacy-by-design for beneficiary systems can be made *executable*: a purpose-bound,
> attribute-level release model, specified as an engine-neutral conformance suite and
> administered as data, can be enforced inside an existing production social protection MIS
> without forking it, and the same artefacts serve as procurement acceptance criteria.

Everything below supports one of the three claims in that sentence: *executable*,
*engine-neutral and administrable*, and *retrofittable without a fork*.

## 2. The gap the work addresses

- Privacy frameworks (NIST Privacy Framework, NIST privacy engineering objectives of
  predictability, manageability and disassociability, ISO 31700) describe properties, not
  mechanisms. There is no reference code a ministry or vendor can run.
- Access control research offers purpose-based access control (allow/deny by purpose), ABAC and
  XACML (with obligations), and data masking. None of them, as deployed in sectoral MIS, returns
  *how much* of an attribute may leave the system for a purpose, with the transformation done by
  the holder of the data.
- The dominant open-source social protection MIS (openIMIS, into which the World Bank's CORE-MIS
  was merged in 2023) authorises by numeric right codes and geography: a permitted user sees
  every field of every record in scope, for every reason, and reads are not audited. Its own
  documentation lists "mark personal data", "audit log of access to personal data" and consent
  as open requirements.
- Digital public infrastructure specifications (GovStack) have Consent and Information Mediator
  building blocks but no building block for purpose-bound minimisation between them.

## 3. Contributions, ranked by novelty

### C1. Release modes as the decision output (the core technical novelty)

The policy decision point does not answer allow/deny. For each requested attribute it returns a
*release mode* from a closed vocabulary (`exact`, `band`, `assertion:<name>`,
`precision:<level>`, `token`, `verify_only`, `masked`, `deny`), plus obligations
(`no_export`, `no_persist`, `expire_response:<ttl>`, `k_anonymity:<k>`, `alert`,
`review_required`, ...) and stable reason codes. The transformation is executed by the service
that holds the raw value, because only it has the value and the program's thresholds.

Why this is new relative to prior art:

- Purpose-based access control (Byun and Li, and successors) binds purposes to data but decides
  allow/deny per object; it does not prescribe a per-attribute transformation.
- XACML obligations and advice attach post-decision duties, but there is no standard vocabulary
  that makes "release the year of the date of birth" or "release whether income is below this
  program's threshold" a decision outcome.
- Dynamic data masking in databases is role-based and static per column; it has no purpose
  input, no program-specific assertions, and no decision log.
- The combination "purpose + program + subject relationship + context ⇒ per-attribute mode +
  obligations + reason code", evaluated by an engine that never sees the data, is the
  contribution. Section 7 of the source guide sketched it; this work gives it a formal
  input/output contract and two independent implementations.

Evidence: `docs/decision-model.md`, `src/pbd_spmis/pdp/engine.py`, `policy/rego/spmis/authz.rego`,
`policy/conformance/vectors.json`; the Appendix A example of the guide reproduces exactly.

### C2. The catalogue as the policy interface (manageability made concrete)

All facts the engines reason about are data: purposes with lawful bases (consent is one basis,
not the default), attribute classification (C1 to C4) with permitted disclosure modes per
attribute, per-purpose release maps with role overrides, programs with thresholds and rules,
context policy, retention schedule and the inter-agency sharing matrix. A JSON Schema plus
cross-reference validation rejects inconsistent catalogues (a mode an attribute does not
permit, an export purpose releasing sensitive data exactly, an override for a role the purpose
does not allow). The catalogue version is the policy version stamped on every decision and audit
event.

Novelty: NIST's "manageability" objective is operationalised as "changing who may see what, in
which form, is a reviewed data change with a validator and tests, not a software release". The
same catalogue drives the Python engine, the Rego policy, the SDK transformations, the openIMIS
module and the gateway.

### C3. Specification by conformance vectors, with two engines that must agree

Thirty-one hand-written vectors encode the privacy tests of the guide (purpose enforcement,
minimisation, cross-program isolation, token unlinkability, export control, retention,
break-glass, session risk) as exact expected decisions. Both a readable Python evaluator and an
OPA/Rego policy must pass them, and the suite is replayed over the HTTP contract, so a third
party's implementation can be held to it.

Novelty: the privacy requirements of a sectoral architecture become an executable acceptance
test that can be written into a tender. The two-engine agreement is a methodological device:
the vectors, not either engine, are the specification.

### C4. "Query, do not copy" as a formal exchange pattern

Inter-agency verification is specified by a sharing matrix (which source may be asked which
question, under which purposes, with which response schema and cache TTL). The broker obtains a
decision for the governing attribute, resolves the identifier just in time from the vault under a
purpose whose obligation is `no_persist`, queries the source, filters the answer through the
response schema, caches the assertion, and records an `external_disclosure` event naming the
source and the keys returned. The national identifier is never written outside the vault; the
tests dump every database to prove it.

Novelty: the guide's principle is given a data-sharing-agreement-shaped configuration and an
auditable protocol; the identifier's lifetime is bounded to a single call by policy, not by
convention.

### C5. Identity separation, program-specific identifiers and break-glass inside the gates

Direct identifiers exist in one envelope-encrypted store with a blind index for deduplication;
every other store is keyed by opaque person tokens or program-specific identifiers that only the
vault can link, under a policy decision and a must-record audit event. Exceptional access is a
workflow (MFA, separation of duties, time-bounded grants scoped to one subject and named
attributes, alert, independent review) whose state is an *input to the decision gates*: the
policy checks that the grant is active, held by the requesting actor and scoped to the subject.

Novelty: break-glass is usually an authorisation bypass; here it is a first-class gate in the
same decision model, with its own reason codes and conformance vectors.

### C6. Retrofit by adapter: a replicable method and an empirical case study

The control plane is integrated into openIMIS without a fork through (a) a declarative mapping
(GraphQL types and fields → catalogue attributes; right codes → PbD roles via ordered rules;
operations → default purposes so existing clients keep working; benefit plan codes → programs),
(b) a graphene middleware taking one decision per entity per request, and (c) an opt-in
identifier vaulting mode plus a migration command for existing records. A privacy gateway
applies the same mapping at the edge for systems that cannot embed the module.

The method was validated inside the genuine openIMIS backend (core 1.11.0, individual 1.4.0,
social protection 1.5.0, Django 4.2, graphene 2, PostgreSQL 16) through openIMIS's own GraphQL
view with openIMIS-issued tokens: 24 of 24 checks. The validation surfaced findings that are
themselves reportable: non-nullable personal fields need a redaction marker rather than null,
typed fields need typed release values (year precision of a `Date` is a date), promise-based
executors need chained transforms, and middleware order interacts with the host's tracer.

Novelty: to our knowledge the first demonstration of purpose-bound, attribute-level minimisation
and read auditing added to a widely deployed open-source social protection MIS without modifying
it, with the integration contract (mapping format) stated generally enough to apply to other
systems.

### C7. Audit as evidence of release, not a copy of data

The audit store is append-only and hash-chained, refuses payloads that carry identifiers or
narrative text (a PII guard), logs its own reads, treats disclosures, token resolutions,
break-glass activations and exports as must-record events that fail closed, and serves the
guide's privacy-operations dashboard counters. Tampering is detected by test.

Novelty here is modest and should be claimed as design discipline: the log records *that*
attributes were released, in which mode, under which policy version, never *what*.

## 4. What is not novel (say so, reviewers will check)

Envelope encryption with per-record keys, HMAC blind indexes, SHA-256 hash chains, OPA as a
policy engine, JWT service identities, k-anonymity suppression, FastAPI microservices, GraphQL
middleware. These are standard building blocks; the paper's claims rest on how they are
composed and on C1 to C6, not on any of them.

## 5. Suggested framing

**Title candidates**

- Executable Privacy-by-Design for Social Protection Information Systems: Purpose-Bound
  Attribute Release, Conformance Vectors and a Retrofit to openIMIS
- From Principles to Enforcement: A Purpose-Bound Data Minimisation Control Plane for
  Beneficiary Registries

**Research questions**

- RQ1 Can minimum-necessary disclosure be expressed as a decision model whose outputs are
  per-attribute release modes, and specified independently of the policy engine?
- RQ2 Can such a model be administered as data by non-developers while remaining verifiable?
- RQ3 Can it be enforced inside an existing production MIS without forking it, and what does the
  retrofit reveal about the host's data model and API?
- RQ4 Does the resulting system satisfy the privacy tests of the reference architecture
  (purpose enforcement, minimisation, isolation, unlinkability, export, retention, break-glass,
  audit integrity)?

**Paper type**: design science / software engineering artefact paper with a case study.
State the artefact, the design rationale (the ADRs in `docs/adr` are citable), the evaluation,
and the limitations.

## 6. Evidence already available for the evaluation section

| Claim | Evidence in the repository |
| --- | --- |
| Decision model is engine-neutral | 31 vectors × {Python engine, Rego policy, HTTP contract}, all passing in CI |
| The guide's privacy tests hold end to end | `tests/test_end_to_end.py`, `tests/test_breakglass_audit_retention.py`, `tests/test_resilience.py` (155+ tests) |
| No identifier leaves the vault | database dumps asserted in tests; broker `no_persist` |
| Fail closed | PDP and audit unavailability tests |
| Retrofit works on real openIMIS | `integrations/openimis/validation` (24/24 checks, exact versions recorded in `last_run.json`) |
| Catalogue validation catches misconfiguration | `tests/test_catalog.py` |
| Operational cost of the retrofit | one decision per entity type per request (design), to be measured (see 8) |

## 7. Limitations to state honestly

- No field deployment with real beneficiaries or real authoritative sources; adapters are mocks
  and retention periods and lawful bases are placeholders.
- The relationship resolver reads beneficiary rows; group-based programs and bulk imports are
  covered partially (imports are vaulted by a migration command, not at import time).
- Performance: decisions are cached per entity per request, but latency under load has not been
  benchmarked. This is the most important measurement to add before submission.
- The openIMIS frontend does not yet send purpose headers; defaults per operation are used, which
  is a weaker purpose signal than an explicit one.
- Single-author artefact; no user study with privacy officers on the catalogue format.
- Re-identification risk in analytics is addressed only by k-anonymity.

## 8. Experiments worth adding before submission

1. **Latency benchmark**: openIMIS list queries of 10, 100, 1,000 individuals with and without
   the middleware; embedded engine vs OPA sidecar. Report median and p95.
2. **Policy change study**: change one release rule in the catalogue, show the vector that must
   change, both engines failing then passing, no code changed; time the cycle.
3. **Expert review**: three to five privacy or data-protection officers assess the catalogue
   format and the audit dashboard against their legal obligations.
4. **Second host**: apply the mapping format to a second system (a FHIR server through the
   gateway is already tested) to support the generality claim of C6.
5. **Attack cases**: enumerate attempts to bypass (wildcard program claims by humans, forged
   grants, export under operational purposes, identifier in audit payload) and show the
   corresponding vector or test.

## 9. Related work to position against (verify current literature before citing)

- Purpose-based access control: Byun and Li, "Purpose based access control for privacy
  protection in relational database systems" (VLDB Journal, 2008) and the Hippocratic database
  line (Agrawal et al., VLDB 2002).
- Attribute-based access control and XACML obligations (NIST SP 800-162; OASIS XACML 3.0).
- Privacy engineering objectives (NIST IR 8062), NIST Privacy Framework, ISO/IEC 31700-1:2023.
- Data minimisation in APIs and GraphQL authorisation (field-level authorisation literature).
- Tokenisation and vault patterns in payment systems (PCI DSS tokenisation guidance).
- Break-glass access control (Brucker and Petritsch, SACMAT 2009).
- Social protection delivery systems and data protection: World Bank Sourcebook on the
  Foundations of Social Protection Delivery Systems (2020); openIMIS data privacy page; GovStack
  Consent and Information Mediator building block specifications.
- Policy as code: Open Policy Agent documentation; Cedar.

## 10. Draft abstract

Social protection management information systems hold the most sensitive data a state collects
about its poorest citizens, yet most of them expose a single rich beneficiary profile protected
only by role checks. Privacy frameworks describe purpose limitation and minimisation; little of
it is available as software. We present PbD-SPMIS, an open reference implementation of a
privacy control plane for beneficiary systems. Its decision model returns, for each requested
attribute, a release mode (exact, band, assertion, reduced precision, token, masked or denied)
together with obligations and stable reason codes, so that the service holding the data releases
only the form a registered purpose permits. The model is administered as a validated catalogue of
purposes with lawful bases, attribute classes, programs, retention and inter-agency sharing
rules, and is specified by thirty-one conformance vectors that a Python evaluator and an
OPA/Rego policy must both satisfy. The control plane adds tokenised identity separation, a
query-do-not-copy exchange broker, break-glass access as a policy gate, and a tamper-evident,
identifier-free audit log. We show that the same control plane can be retrofitted to the most
widely deployed open-source social protection platform, openIMIS, through a declarative mapping
and a GraphQL middleware, validated inside the genuine openIMIS backend with twenty-four checks,
and we report the host-specific findings the retrofit exposed. The artefacts are released under
open licences and double as procurement acceptance criteria.
