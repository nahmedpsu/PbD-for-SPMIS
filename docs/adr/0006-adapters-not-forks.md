# ADR 0006: Integrate with existing MIS products through adapters, not forks

**Status:** accepted · **Date:** 2026-10-07

## Context

Countries run existing social protection MIS products, chiefly openIMIS (into which the World
Bank's CORE-MIS was merged in 2023). They will not replace them with a reference implementation,
but they need the privacy controls the reference implements. Three ways were considered: a
native openIMIS module, a privacy gateway in front of any API, and a product-neutral control
plane API with thin adapters.

## Decision

Treat the control plane as the product and integrate through two thin adapters that share one
SDK and one mapping format:

- `openimis-be-pbd`, an openIMIS backend module built on graphene's middleware contract, with
  optional identifier vaulting and service-signal auditing;
- the privacy gateway, a reverse proxy for GraphQL and FHIR/REST APIs of systems that cannot
  embed the module.

Both derive roles from the host's own authorisation data (openIMIS right codes), default the
purpose per operation so existing clients keep working, take one decision per entity per
request, audit reads, and fail closed. Neither forks or modifies the host product.

## Consequences

- openIMIS deployments adopt privacy-by-design by adding a module and a header, and can enable
  vaulting when they are ready to change their data layout.
- The gateway covers legacy and third-party systems at the cost of only seeing responses (no
  vaulting, no visibility of internal jobs); its limits are documented rather than hidden.
- The mapping file becomes a per-deployment artefact that privacy officers review alongside the
  catalogue.
- Vaulting is off by default: it is the strongest gain but a data-layout change that a country
  must opt into.
