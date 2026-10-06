# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting on this repository ("Report a vulnerability" under the Security tab). Include the
affected component (service, catalogue, policy), a reproduction, and the impact on
confidentiality of beneficiary data. We aim to acknowledge within five working days.

## Scope

In scope: the policy engines and their agreement, the PEP transformations, the vault and payment
encryption, the audit chain, the break-glass workflow, the retention engine, the broker's
handling of resolved identifiers, and the catalogue validation.

Out of scope for this reference implementation (documented in `docs/deployment.md` as
production responsibilities): the development HS256 token issuer, the local key provider, the
mock authoritative sources and the mock payment provider.

## Development defaults are not secrets

`PBD_AUTH_SIGNING_KEY`, `PBD_VAULT_MASTER_KEY` and `PBD_INDEX_HMAC_KEY` have insecure defaults so
the demo runs without setup. The vault reports `key_id: insecure-dev-default` on `/healthz` when
no master key is configured. Never deploy with those defaults.
