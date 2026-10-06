# Conformance suite

`policy/conformance/vectors.json` is the executable acceptance test for the authorisation model.
It exists so that:

- the embedded Python engine and the Rego policy cannot drift apart;
- a government can put "passes the PbD-SPMIS conformance vectors" into a tender and a vendor can
  prove it with one command;
- every privacy test in section 14.1 of the guide has a concrete, replayable form.

## Format

```json
{
  "name": "cross_program_isolation",
  "description": "A Program A user cannot read Program B data without an assignment.",
  "guide_ref": "14.1 Cross-program isolation",
  "input": { "actor": …, "subject": …, "program": …, "purpose": …, "action": …, "attributes": […], "context": {…} },
  "expected": {
    "allow": false,
    "release": {"income": "deny"},
    "reason_codes": ["NO_PROGRAM_ASSIGNMENT"],
    "obligations_include": ["log"],
    "obligations_exclude": []
  }
}
```

`allow`, `release` and `reason_codes` must match exactly. `obligations_include` must be a subset
of the decision's obligations and `obligations_exclude` disjoint from them.

## Running

```bash
python -m pytest tests/test_conformance.py        # embedded engine, Rego via `opa eval`, PDP over HTTP
opa test --v1-compatible policy/rego policy/bundle -v   # Rego's own test harness
pbd-spmis decide input.json                         # evaluate one input with the embedded engine
```

The Rego runs are skipped automatically when no `opa` binary is on the PATH; CI installs one.

## Coverage

| Guide test (14.1) | Vectors |
| --- | --- |
| Purpose enforcement | `purpose_enforcement_same_actor_disallowed_purpose`, `purpose_enforcement_allowed_purpose_denies_field`, `unknown_purpose_is_denied`, `purpose_not_registered_for_program` |
| Minimisation | `appendix_a_case_worker_eligibility`, `appendix_a_with_catalogue_attribute_names`, `registration_write_then_read_back_is_minimised` |
| Cross-program isolation | `cross_program_isolation`, `subject_not_related_to_program`, `wildcard_program_only_for_service_identities`, `service_role_claim_without_service_flag` |
| Token unlinkability | `token_unlinkability_for_program_services`, `broker_just_in_time_resolution`, `payment_service_receives_token_not_account` |
| Export controls | `analyst_export_is_deidentified`, `export_denied_for_operational_purpose` |
| Break-glass | `break_glass_requires_grant`, `break_glass_requires_mfa`, `break_glass_active_grant_scoped`, `break_glass_expired_grant`, `break_glass_not_holder`, `break_glass_subject_mismatch` |
| Session risk / context | `low_trust_device_downgrades_c4`, `low_trust_device_nothing_releasable` |
| Relationship-based case access | `case_management_requires_case_assignment`, `case_management_with_assignment` |
| Role overrides | `finance_officer_role_override` |
| Default deny / fail closed | `unauthenticated_request`, `unknown_role`, `unknown_attribute_is_denied_individually` |
| Retention | `retention_service_delete` |

Retention execution, audit integrity, fail-closed on dependency loss and "no identifier ever
lands outside the vault" are service-level properties and are covered by the end-to-end tests
in `tests/` rather than by vectors.

## Adding a vector

1. Write the input and the expected outcome by hand from the catalogue (do not generate the
   expectation from an engine: the vector is the specification).
2. `python scripts/build_bundle.py` to refresh the OPA bundle.
3. `python -m pytest tests/test_conformance.py`. If only one engine fails, that engine is wrong.

## Using the vectors against another implementation

Any implementation that exposes `POST /v1/decisions` with the documented input and output can
be tested with `tests/test_conformance.py::test_pdp_http_contract_vector` by pointing
`PBD_PDP_URL` at it. The reason codes and release modes are part of the contract.
