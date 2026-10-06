# ADR 0002: The catalogue is the policy interface

**Status:** accepted · **Date:** 2026-10-06

## Context

"Manageability" in the guide means privacy administrators can change purposes, retention,
notices and sharing rules without rewriting applications. If release rules live in code or in
Rego, every change is a software release.

## Decision

All facts the engines reason about live in `catalog/*.yaml`: attributes with classes and
permitted disclosure modes, purposes with lawful bases, allowed roles, actions, release maps and
role overrides, programs with thresholds and rules, context policy, retention schedule and the
sharing matrix. A JSON Schema plus cross-reference validation rejects inconsistent catalogues.
The merged catalogue is exported verbatim as OPA's `data.catalog`, and its version is the policy
version stamped on every decision and audit event.

## Consequences

- Changing who may see what is a reviewed data change with a validator and tests, not a code
  change.
- Both engines are generic over the catalogue; adding a purpose or program needs no engine edit.
- Context rules that need new logic (not new data) still require engine changes, guarded by
  conformance vectors.
