# The decision model

The authorisation model is specified three times, deliberately: as prose here, as the embedded
Python evaluator (`src/pbd_spmis/pdp/engine.py`, ~200 lines) and as the Rego policy
(`policy/rego/spmis/authz.rego`). The conformance vectors keep the three in agreement.

## Input

```json
{
  "actor":   {"id": "cw-1", "role": "CASE_WORKER", "agency": "SOCIAL_PROTECTION_AGENCY",
              "office": "North", "programs": ["cash_assistance"], "cases": ["CASE-1"],
              "amr": ["pwd", "mfa"], "is_service": false},
  "subject": {"person_token": "P-8294AX", "program_relationship": ["cash_assistance"]},
  "program": "cash_assistance",
  "purpose": "eligibility_verification",
  "action":  "read",
  "attributes": ["income", "household_size", "disability_status", "bank_account", "address"],
  "context": {"channel": "api", "device_trust": "managed", "case_id": null,
              "break_glass": {"active": true, "grant_id": "BG-1", "actor_id": "cw-1",
                              "matches_subject": true, "attributes": ["contact"]}}
}
```

The input never contains personal data: tokens, roles, purposes and attribute *names* only.

## Gates

Evaluated in order; the first failure is the decision's single reason code and every attribute
is denied with `GATE_DENIED`.

| # | Gate | Reason code |
| --- | --- | --- |
| 1 | actor has an id | `AUTH_REQUIRED` |
| 2 | role is a known human or service role (`policy.yaml`) | `UNKNOWN_ROLE` |
| 3 | purpose is registered | `UNKNOWN_PURPOSE` |
| 4 | program is registered | `UNKNOWN_PROGRAM` |
| 5 | purpose is listed by the program | `PURPOSE_NOT_ALLOWED_FOR_PROGRAM` |
| 6 | role is in the purpose's `allowed_roles` | `ROLE_NOT_PERMITTED` |
| 7 | action is in the purpose's `actions` | `ACTION_NOT_PERMITTED` |
| 8 | actor is assigned to the program (`*` only for service identities) | `NO_PROGRAM_ASSIGNMENT` |
| 9 | if `requires_relationship`: subject is related to the program | `NO_SUBJECT_RELATIONSHIP` |
| 10 | if `requires_case_assignment` (humans): `context.case_id` is in `actor.cases` | `NO_CASE_ASSIGNMENT` |
| 11 | if `requires_mfa` (humans): `"mfa"` in `actor.amr` | `MFA_REQUIRED` |
| 12 | if `requires_break_glass`: a grant object is present | `BREAK_GLASS_REQUIRED` |
| 13 | … and active | `BREAK_GLASS_INACTIVE` |
| 14 | … and held by this actor | `BREAK_GLASS_NOT_HOLDER` |
| 15 | … and scoped to this subject | `BREAK_GLASS_SUBJECT_MISMATCH` |

## Per-attribute release

For each requested attribute:

1. Unknown attribute → `deny` / `UNKNOWN_ATTRIBUTE` (the request as a whole is not failed).
2. Break-glass purpose → `exact` / `BREAK_GLASS_RELEASE` if the attribute is in the grant's
   scope, else `deny` / `OUT_OF_GRANT_SCOPE`.
3. Otherwise the mode from the purpose's `role_overrides[role]` (`ROLE_OVERRIDE`) or
   `release` map (`RELEASED`); absent means `deny` / `NOT_IN_PURPOSE`.
4. If `context.device_trust == "low"` and the attribute's class is in
   `policy.low_trust_denies_classes`, the mode becomes `deny` / `LOW_TRUST_DEVICE`.

## Allow

- `read` and `export`: allowed only if at least one attribute is released; otherwise
  `NOTHING_RELEASABLE`.
- `write` and `delete`: allowed by the gates alone (the release map still describes what the
  writer may read back).

## Obligations

The purpose's obligations, plus `log` always, plus `no_export` when an attribute of a class in
`policy.no_export_classes` is released *exactly* (bands, assertions and tokens are already
minimised), plus `alert` and `review_required` for break-glass purposes. Obligations are sorted,
returned in the decision, written to the audit event and sent to the caller as `X-Obligations`.
Services enforce the ones they can (`no_export` → `Cache-Control: no-store`; `k_anonymity:N` →
small-group suppression; `no_persist` → the broker keeps resolved identifiers in memory only).

## Output

```json
{
  "decision_id": "PD-…", "allow": true, "policy_version": "2026.10.0", "engine": "embedded",
  "release": {"income": "assertion:below_threshold", "household_size": "exact",
              "disability_status": "assertion:eligible", "bank_account": "deny", "address": "precision:district"},
  "obligations": ["expire_response:24h", "log", "no_export"],
  "reason_codes": ["ALLOW"],
  "attribute_reasons": {"income": "RELEASED", "bank_account": "NOT_IN_PURPOSE", "…": "…"},
  "evaluated_at": "2026-10-06T19:53:45Z"
}
```

`decision_id`, `engine` and `evaluated_at` are added by the PDP service; the rest is the engine
output and is identical between the embedded engine and OPA.

## Changing policy

- To let a role see a new attribute under a purpose: edit `catalog/purposes.yaml`, run
  `pbd-spmis catalog validate`, add or update a conformance vector, run
  `python scripts/build_bundle.py`, run the tests. No application code changes.
- To add a program: add it to `catalog/programs.yaml` with its purposes, thresholds and rules.
- To add an authoritative source: add it to `catalog/sharing.yaml` and implement an adapter in
  `src/pbd_spmis/broker/adapters`.
- To add a context rule (e.g. deny C4 outside office hours): extend `policy.yaml`, the Python
  engine, the Rego policy, and add vectors. The conformance test will fail until both engines agree.

The catalogue version is the policy version. It appears on every decision and every audit event,
so a reviewer can always answer "which rules were in force when this was released?".
