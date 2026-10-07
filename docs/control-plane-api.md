# Privacy Control Plane API v1

The control plane is consumed by the reference SP-MIS services, by the SDK, by the openIMIS
module and by the gateway. These endpoints are the stable surface for integrators.

## Compatibility promise

- Paths below are versioned `/v1`; fields may be added, never removed or re-typed within v1.
- Reason codes, release modes and obligation names are part of the contract
  ([decision-model.md](decision-model.md)); new ones may be added.
- The catalogue version returned as `policy_version` changes independently of the API version.
- Generated OpenAPI documents live in `contracts/openapi/` and are checked in CI against the code.

## Endpoints

| Service | Endpoint | Purpose |
| --- | --- | --- |
| pdp | `POST /pdp/v1/decisions` | purpose-bound, attribute-level decision |
| pdp | `GET /pdp/v1/catalog` | non-sensitive catalogue view (classes, bands, thresholds, roles) used by clients to apply release modes locally |
| pdp | `GET /pdp/v1/policy` | policy version, engine, registered purposes/programs/attributes |
| vault | `POST /vault/v1/identities` | identity proofing: create or find a person token (dedup by blind index) |
| vault | `GET /vault/v1/identities/{token}/status` | verified assertion, no identifiers |
| vault | `POST /vault/v1/program-identifiers` | program-specific identifier |
| vault | `POST /vault/v1/resolve` | policy-gated release of identifiers (services, or humans under break-glass) |
| broker | `POST /broker/v1/verify` | assertions from authoritative sources ("query, do not copy") |
| audit | `POST /audit/v1/events` | append an event (PII-guarded, hash-chained) |
| audit | `GET /audit/v1/events`, `/v1/chain/verify`, `/v1/metrics` | read, verify, dashboard (auditor roles) |
| breakglass | `POST /breakglass/v1/grants`, `.../approve`, `.../revoke`, `.../review`, `GET .../{id}` | exceptional access workflow |
| retention | `POST /retention/v1/schedules`, `POST /retention/v1/run`, `GET /retention/v1/schedules` | retention registration and execution |

Request headers on sensitive endpoints: `Authorization: Bearer`, `X-Purpose`, `X-Program`,
optional `X-Correlation-ID`, `X-Channel`, `X-Device-Trust`, `X-Case-ID`, `X-Break-Glass-Grant`.
Response headers: `X-Decision-ID`, `X-Policy-Version`, `X-Obligations`.

## Using the SDK

```python
from pbd_spmis.sdk import PrivacyControlPlane, PolicyDeniedError, UnavailableError

cp = PrivacyControlPlane("https://privacy.example.gov", token=lambda: service_token())

actor = {"id": "cw-1", "role": "CASE_WORKER", "agency": "SPA", "programs": ["cash_assistance"], "amr": ["pwd"]}
try:
    d = cp.pdp.enforce(actor=actor, program="cash_assistance", purpose="eligibility_verification",
                       attributes=["income", "address"], subject_token="P-8294AX",
                       subject_programs=["cash_assistance"])
except PolicyDeniedError as e:
    ...  # e.reason_codes, e.decision_id
except UnavailableError:
    ...  # fail closed

view = cp.apply_release({"income": 180, "address": {"district": "North", "street": "..."}}, d,
                        program="cash_assistance")
# {"income": True, "address": {"district": "North"}}
cp.audit.emit("read_access", actor_id="cw-1", outcome="allow", decision_id=d.decision_id,
              attributes=d.released(), obligations=d.obligations)
```

The SDK depends on `httpx` and `pyyaml` only. Transformations run client-side from the cached
catalogue view, so an adapter does not need the catalogue files.

## Conformance for third-party implementations

Any implementation of `POST /v1/decisions` can be checked against the vectors in
`policy/conformance/vectors.json` by pointing `PBD_PDP_URL` at it and running
`tests/test_conformance.py::test_pdp_http_contract_vector`.
