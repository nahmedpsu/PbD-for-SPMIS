# ADR 0003: Release modes and obligations are part of the API contract

**Status:** accepted · **Date:** 2026-10-06

## Context

Allow/deny is not enough for minimum necessary disclosure. The decision has to say *how much*
of each attribute may be released, and what the caller must do with it.

## Decision

The PDP returns a per-attribute release mode from a closed vocabulary (`exact`, `band`,
`assertion:<name>`, `precision:<level>`, `token`, `verify_only`, `masked`, `deny`) and a list of
obligations. The data-holding service applies the transformation (only it has the raw value and
the program's thresholds) and forwards the obligations to the caller as `X-Obligations`. Reason
codes are stable strings. All three are documented in the OpenAPI contracts and exercised by the
conformance vectors.

## Consequences

- Clients can rely on the shape of a decision across implementations.
- Obligations the services can enforce locally (`no_export`, `k_anonymity:N`, `no_persist`) are
  enforced; the rest are recorded in the audit log and are the caller's responsibility.
- New modes require adding a transformation in `pep.transform` and a disclosure entry for the
  attributes they apply to.
