# Conformance tests for the Rego policy.
#
# `test_conformance_vectors` replays every vector in data.conformance.vectors (built into
# policy/bundle/data.json from policy/conformance/vectors.json). The named tests below pin the
# most important properties individually so a failure is easy to read.
#
# Run:  opa test policy/rego policy/bundle -v

package spmis.authz_test

import rego.v1

import data.spmis.authz

vector_passes(v) if {
	d := authz.decision with input as v.input
	d.allow == v.expected.allow
	d.release == v.expected.release
	d.reason_codes == v.expected.reason_codes
	every o in v.expected.obligations_include {
		o in d.obligations
	}
	every o in object.get(v.expected, "obligations_exclude", []) {
		not o in d.obligations
	}
}

failing_vectors := {v.name | some v in data.conformance.vectors; not vector_passes(v)}

test_conformance_vectors if {
	count(data.conformance.vectors) > 0
	count(failing_vectors) == 0
}

appendix_a := {
	"actor": {"id": "cw-1", "role": "CASE_WORKER", "agency": "SOCIAL_PROTECTION_AGENCY", "programs": ["cash_assistance"], "amr": ["pwd"]},
	"subject": {"person_token": "P-8294AX", "program_relationship": ["cash_assistance"]},
	"program": "cash_assistance",
	"purpose": "eligibility_verification",
	"action": "read",
	"attributes": ["income", "household_size", "disability_status", "bank_account"],
	"context": {"device_trust": "managed"},
}

test_appendix_a_example if {
	d := authz.decision with input as appendix_a
	d.allow
	d.release.income == "assertion:below_threshold"
	d.release.household_size == "exact"
	d.release.disability_status == "assertion:eligible"
	d.release.bank_account == "deny"
	"log" in d.obligations
	"no_export" in d.obligations
}

test_default_deny_unknown_purpose if {
	d := authz.decision with input as object.union(appendix_a, {"purpose": "does_not_exist"})
	not d.allow
	d.reason_codes == ["UNKNOWN_PURPOSE"]
}

test_cross_program_isolation if {
	d := authz.decision with input as object.union(appendix_a, {"program": "disability_allowance"})
	not d.allow
	d.reason_codes == ["NO_PROGRAM_ASSIGNMENT"]
}

test_low_trust_device_never_receives_c4 if {
	d := authz.decision with input as object.union(appendix_a, {"context": {"device_trust": "low"}})
	d.allow
	d.release.income == "deny"
	d.release.household_size == "exact"
	d.attribute_reasons.income == "LOW_TRUST_DEVICE"
}

test_fail_closed_without_actor if {
	d := authz.decision with input as object.union(appendix_a, {"actor": {"id": ""}})
	not d.allow
	d.reason_codes == ["AUTH_REQUIRED"]
}

test_break_glass_requires_active_grant_held_by_actor if {
	base := object.union(appendix_a, {
		"purpose": "emergency_protection",
		"actor": {"id": "cw-1", "role": "CASE_WORKER", "programs": ["cash_assistance"], "amr": ["pwd", "mfa"]},
		"attributes": ["contact", "national_id"],
	})
	no_grant := authz.decision with input as base
	no_grant.reason_codes == ["BREAK_GLASS_REQUIRED"]

	wrong_holder := authz.decision with input as object.union(base, {"context": {"break_glass": {"active": true, "actor_id": "someone-else", "matches_subject": true, "attributes": ["contact"]}}})
	wrong_holder.reason_codes == ["BREAK_GLASS_NOT_HOLDER"]

	ok := authz.decision with input as object.union(base, {"context": {"break_glass": {"active": true, "actor_id": "cw-1", "matches_subject": true, "attributes": ["contact"]}}})
	ok.allow
	ok.release.contact == "exact"
	ok.release.national_id == "deny"
	"alert" in ok.obligations
	"review_required" in ok.obligations
}
