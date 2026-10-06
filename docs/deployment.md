# Deployment and production hardening

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PBD_SERVICE_MODE` | `allinone` | `allinone` dispatches inter-service calls in-process; `distributed` uses `PBD_<SERVICE>_URL` |
| `PBD_<SERVICE>_URL` | – | base URL of a service in distributed mode (`PBD_PDP_URL`, `PBD_AUDIT_URL`, …) |
| `PBD_DATABASE_URL` | `sqlite:///./data/{service}.sqlite3` | SQLAlchemy URL template; `{service}` is substituted so every store is its own database |
| `PBD_CATALOG_DIR` | `./catalog` | directory with the catalogue YAML files |
| `PBD_POLICY_ENGINE` | `embedded` | `embedded` or `opa` |
| `PBD_OPA_URL` | `http://localhost:8181` | OPA address when the engine is `opa` |
| `PBD_AUTH_SIGNING_KEY` | dev value | HS256 key for development tokens |
| `PBD_AUTH_ISSUER` | `pbd-spmis-dev` | expected token issuer |
| `PBD_VAULT_MASTER_KEY` | derived dev key | base64 32-byte master key wrapping per-record keys (vault, payments) |
| `PBD_INDEX_HMAC_KEY` | dev value | HMAC key for the national-id blind index |
| `PBD_FAIL_CLOSED` | `true` | sensitive operations fail when the PDP is unavailable |

## Modes

**All-in-one.** `pbd-spmis serve all`. One process, one SQLite file per service under `./data`.
For development, demos and the test-suite.

**Compose.** `docker compose up --build`. PostgreSQL 16 with one database per service
(`deploy/postgres/init.sql`), OPA 1.9 serving `policy/rego` and `policy/bundle`, the services in
one container with `PBD_POLICY_ENGINE=opa`.

**Distributed.** `docker compose --profile distributed up`. One container per service from the
same image (`pbd-spmis serve <service>`), wired by `PBD_*_URL`. This is the shape to map onto
Kubernetes, with the services placed in the guide's zones:

| Zone (guide 11.1) | Services |
| --- | --- |
| Application | registry, program, eligibility, payments |
| Privacy services | pdp (+ OPA sidecar), vault, breakglass, retention |
| Data | PostgreSQL databases, object storage |
| Integration | broker and its adapters |
| Management | audit (read side), dashboards |
| Analytics | export consumers |

## Production hardening checklist

The reference implementation makes the production seams explicit. Before any real data:

1. **Identity provider.** Replace `common/auth.verify_token` with JWKS validation against the
   government IdP or Keycloak (OIDC). Map claims to `Actor` (role, agency, programs, cases,
   `amr`). Issue service identities as client credentials with `svc=true`; prefer mTLS between
   services.
2. **Keys.** Implement `KeyProvider.wrap/unwrap` against the KMS/HSM so the master key never
   enters the process. Rotate by re-wrapping DEKs (`FieldCipher.wrapped_dek`), no data rewrite.
   Set `PBD_INDEX_HMAC_KEY` from the secrets store.
3. **Databases.** One PostgreSQL database and role per service; the vault database on its own
   instance or at least its own role with no cross-database grants. Enable TLS, encryption at
   rest, backups with restore tests. The audit database role used by services must be
   INSERT-only; a separate read role for auditors.
4. **Policy distribution.** Serve the Rego policy and bundle from an OPA bundle server with
   signatures; run OPA as a sidecar of the PDP; keep the catalogue in version control with
   review by the privacy function; the catalogue version is the policy version on every
   decision.
5. **Gateway.** Terminate TLS, enforce rate limits, schema validation and size limits; strip
   client-supplied `X-Device-Trust` and set it from device posture; set `X-Channel`.
6. **Logging.** Application logs must not contain request bodies of sensitive endpoints; the
   audit store's PII guard is a backstop, not the control. Export audit events to the SIEM.
7. **Adapters.** Replace mock sources with signed, mutually authenticated calls under the data
   sharing agreement recorded in `catalog/sharing.yaml`; keep the response schema filter.
8. **Retention.** Run `POST /retention/v1/run` from a scheduler with the retention service
   identity; review `due_within_days` on the dashboard; keep the legal periods in the catalogue.
9. **Break-glass alerts.** Wire the `break_glass_activated` audit event to the SOC pager.
10. **Load and DR.** The PEP adds one PDP round trip per request; deploy the PDP close to the
    services (sidecar OPA) and verify latency under load as the guide's NFR requires.
