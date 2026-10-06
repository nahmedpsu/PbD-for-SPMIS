# ADR 0004: Fail closed on the PDP; must-record audit events

**Status:** accepted · **Date:** 2026-10-06

## Context

The guide states that if the policy engine is unavailable, sensitive operations should fail
closed, and that every disclosure and exceptional access must be attributable.

## Decision

- The PEP raises `upstream_unavailable` (HTTP 503) when the PDP cannot be reached or returns an
  unexpected response. There is no silent default-allow and no cached "last decision".
- Audit events are best-effort in general, but `external_disclosure`, `token_resolved`,
  `break_glass_activated` and `export` are *must-record*: if the audit store rejects or cannot
  receive them, the operation fails and nothing is released.

## Consequences

- Availability of the PDP and the audit store is a privacy control, so they are deployed as
  sidecar/near services with their own SLOs.
- Any continuity mode for essential services must be designed explicitly (pre-approved, narrow,
  time-bounded, audited) rather than emerging from a timeout.
