# Announcement drafts

## Mailing list / forum post (openIMIS community, DPGA, social protection practitioners)

**Subject:** Open-source Privacy-by-Design control plane for social protection MIS, with an openIMIS module

Social protection systems hold the most sensitive data a state collects about its poorest
citizens, and most of them still rely on a single rich beneficiary profile protected by role
checks. Privacy frameworks describe what good looks like; very little ships as code.

I have published PbD-SPMIS, an open reference implementation of privacy-by-design for
beneficiary systems: https://github.com/nahmedpsu/PbD-for-SPMIS

What it is:

- A privacy control plane: a purpose registry and attribute catalogue, a policy decision point
  that returns per-attribute release modes (exact, band, assertion, reduced precision, masked,
  denied) with obligations, an identity vault with tokenisation, a "query, do not copy"
  exchange broker, break-glass as a workflow, a tamper-evident audit log and a retention engine.
- A reference SP-MIS on top (registry, eligibility, enrollment, payments) proving the chain.
- 31 conformance vectors that both a Python engine and an OPA/Rego policy pass, so the rules
  can be put in a tender and verified.
- An openIMIS backend module (`openimis-be-pbd`) that brings purpose-bound minimisation, read
  auditing, fail-closed authorisation and optional identifier vaulting to openIMIS and CORE-MIS
  deployments by adding one module and one request header. A gateway covers systems that
  cannot embed the module.

Everything is Apache-2.0 (the openIMIS module AGPL-3.0), tested, and runs with one command.

I am looking for: reviewers of the openIMIS mapping and right-code rules, a deployment willing to
pilot the module on a test instance, and feedback from privacy and data-protection officers on
the catalogue format. Replies here or issues on the repository are welcome.

## Short form (social / chat)

PbD-SPMIS: an open, tested privacy-by-design control plane for social protection MIS. Purpose-
bound attribute-level release, tokenised identity, query-do-not-copy exchange, break-glass,
tamper-evident audit, retention. Conformance vectors any vendor can be held to. Plus an openIMIS
module that adds all of it to existing deployments without a fork.
https://github.com/nahmedpsu/PbD-for-SPMIS
