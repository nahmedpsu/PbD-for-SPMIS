# ADR 0001: Two policy engines, one conformance suite

**Status:** accepted · **Date:** 2026-10-06

## Context

The guide recommends policy-as-code (OPA is named as a suitable option) but also requires that
the privacy model be understandable and testable by people who are not Rego authors: privacy
officers, auditors, procurement reviewers. A single Rego implementation is opaque to them; a
single Python implementation is not what production deployments want at enforcement points.

## Decision

Implement the decision model twice, as a small pure-Python evaluator (the readable
specification, also the default engine for development) and as a Rego policy (the production
policy-as-code form), and make a hand-written conformance vector suite the single source of
truth that both must satisfy. CI replays every vector against the Python engine, against OPA via
`opa eval`, and through the PDP HTTP contract.

## Consequences

- Any change to the model must be made in both engines; the vectors catch divergence immediately.
- The vectors become a portable acceptance test that other implementations can be held to.
- The cost is one duplicated ~200-line evaluator, which we consider worth the readability and
  the engine-neutrality it buys.
