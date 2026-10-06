# ADR 0005: Real service boundaries, in-process composition for development

**Status:** accepted · **Date:** 2026-10-06

## Context

The architecture needs physically separable stores and distinct service identities, but a
reference implementation must be runnable with `pip install` and one command, and testable in
seconds.

## Decision

Each service is an independent FastAPI application with its own database, models and machine
identity, and talks to the others only over HTTP contracts. An httpx transport that drives the
target ASGI application on a background event loop lets the all-in-one mode dispatch those calls
in-process without changing a line of service code. `PBD_SERVICE_MODE=distributed` switches the
same client to real HTTP.

## Consequences

- Tests and the demo exercise the exact request/response contracts of a distributed deployment.
- No service can reach into another's database or models; the only coupling is the contract.
- The in-process transport is development tooling; it must never be used to bypass the gateway
  in production.
