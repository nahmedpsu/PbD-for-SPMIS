# PbD-SPMIS purpose-bound, attribute-level authorisation policy.
#
# This Rego policy is the production policy-as-code form of the authorisation model. It reads the
# privacy catalogue from data.catalog (built from catalog/*.yaml by `pbd-spmis catalog bundle`)
# and must produce exactly the same decision as the embedded Python reference evaluator for every
# vector in policy/conformance/vectors.json.
#
# Query:  data.spmis.authz.decision
# Input:  { actor, subject, program, purpose, action, attributes, context }
# Output: { allow, policy_version, release, obligations, reason_codes, attribute_reasons }

package spmis.authz

import rego.v1

catalog := data.catalog

policy := catalog.policy

actor := object.get(input, "actor", {})

subject := object.get(input, "subject", {})

ctx := object.get(input, "context", {})

purpose_name := object.get(input, "purpose", "")

program_name := object.get(input, "program", "")

action := object.get(input, "action", "read")

attributes := object.get(input, "attributes", [])

role := object.get(actor, "role", "")

purpose := catalog.purposes[purpose_name]

program := catalog.programs[program_name]

bg := b if {
	b := object.get(ctx, "break_glass", {})
	b != null
}

bg := {} if {
	object.get(ctx, "break_glass", {}) == null
}

is_service if {
	actor.is_service == true
	role in policy.service_roles
}

actor_programs := {p | some p in object.get(actor, "programs", [])}

wildcard if {
	is_service
	"*" in actor_programs
}

# ---------------------------------------------------------------------------------------------
# Gates (evaluated in this order; the first failure is the decision's reason code)
# ---------------------------------------------------------------------------------------------

gate_order := [
	"AUTH_REQUIRED",
	"UNKNOWN_ROLE",
	"UNKNOWN_PURPOSE",
	"UNKNOWN_PROGRAM",
	"PURPOSE_NOT_ALLOWED_FOR_PROGRAM",
	"ROLE_NOT_PERMITTED",
	"ACTION_NOT_PERMITTED",
	"NO_PROGRAM_ASSIGNMENT",
	"NO_SUBJECT_RELATIONSHIP",
	"NO_CASE_ASSIGNMENT",
	"MFA_REQUIRED",
	"BREAK_GLASS_REQUIRED",
	"BREAK_GLASS_INACTIVE",
	"BREAK_GLASS_NOT_HOLDER",
	"BREAK_GLASS_SUBJECT_MISMATCH",
]

gate_fails contains "AUTH_REQUIRED" if object.get(actor, "id", "") == ""

gate_fails contains "UNKNOWN_ROLE" if {
	not role in policy.service_roles
	not role in policy.human_roles
}

gate_fails contains "UNKNOWN_PURPOSE" if not catalog.purposes[purpose_name]

gate_fails contains "UNKNOWN_PROGRAM" if not catalog.programs[program_name]

gate_fails contains "PURPOSE_NOT_ALLOWED_FOR_PROGRAM" if not purpose_name in program.purposes

gate_fails contains "ROLE_NOT_PERMITTED" if not role in purpose.allowed_roles

gate_fails contains "ACTION_NOT_PERMITTED" if not action in purpose.actions

gate_fails contains "NO_PROGRAM_ASSIGNMENT" if {
	not wildcard
	not program_name in actor_programs
}

gate_fails contains "NO_SUBJECT_RELATIONSHIP" if {
	purpose.requires_relationship == true
	not program_name in object.get(subject, "program_relationship", [])
}

case_assigned if {
	cid := object.get(ctx, "case_id", "")
	cid != null
	cid != ""
	cid in object.get(actor, "cases", [])
}

gate_fails contains "NO_CASE_ASSIGNMENT" if {
	purpose.requires_case_assignment == true
	not is_service
	not case_assigned
}

gate_fails contains "MFA_REQUIRED" if {
	purpose.requires_mfa == true
	not is_service
	not "mfa" in object.get(actor, "amr", [])
}

bg_required if purpose.requires_break_glass == true

gate_fails contains "BREAK_GLASS_REQUIRED" if {
	bg_required
	count(bg) == 0
}

gate_fails contains "BREAK_GLASS_INACTIVE" if {
	bg_required
	count(bg) > 0
	not bg.active == true
}

gate_fails contains "BREAK_GLASS_NOT_HOLDER" if {
	bg_required
	bg.active == true
	object.get(bg, "actor_id", "") != object.get(actor, "id", "")
}

gate_fails contains "BREAK_GLASS_SUBJECT_MISMATCH" if {
	bg_required
	bg.active == true
	object.get(bg, "actor_id", "") == object.get(actor, "id", "")
	not bg.matches_subject == true
}

failures := [c | some c in gate_order; gate_fails[c]]

gate := failures[0]

# ---------------------------------------------------------------------------------------------
# Per-attribute release
# ---------------------------------------------------------------------------------------------

overrides := object.get(object.get(purpose, "role_overrides", {}), role, {})

low_trust if object.get(ctx, "device_trust", "") == "low"

bg_attrs := {a | some a in object.get(bg, "attributes", [])}

reason_for_mode(m) := "RELEASED" if m != "deny"

reason_for_mode(m) := "NOT_IN_PURPOSE" if m == "deny"

base_release(attr) := [overrides[attr], "ROLE_OVERRIDE"] if overrides[attr]

base_release(attr) := [m, reason_for_mode(m)] if {
	not overrides[attr]
	m := object.get(object.get(purpose, "release", {}), attr, "deny")
}

downgraded(attr) if {
	b := base_release(attr)
	b[0] != "deny"
	low_trust
	catalog.attributes[attr].class in policy.low_trust_denies_classes
}

release_for(attr) := ["deny", "UNKNOWN_ATTRIBUTE"] if not catalog.attributes[attr]

release_for(attr) := ["exact", "BREAK_GLASS_RELEASE"] if {
	catalog.attributes[attr]
	bg_required
	attr in bg_attrs
}

release_for(attr) := ["deny", "OUT_OF_GRANT_SCOPE"] if {
	catalog.attributes[attr]
	bg_required
	not attr in bg_attrs
}

release_for(attr) := ["deny", "LOW_TRUST_DEVICE"] if {
	catalog.attributes[attr]
	not bg_required
	downgraded(attr)
}

release_for(attr) := base_release(attr) if {
	catalog.attributes[attr]
	not bg_required
	not downgraded(attr)
}

release := {a: release_for(a)[0] | some a in attributes}

attribute_reasons := {a: release_for(a)[1] | some a in attributes}

released := {a | some a in attributes; release_for(a)[0] != "deny"}

# ---------------------------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------------------------

exact_sensitive_released if {
	some a in released
	release_for(a)[0] == "exact"
	catalog.attributes[a].class in policy.no_export_classes
}

obligation_set contains o if some o in object.get(purpose, "obligations", [])

obligation_set contains "log"

obligation_set contains "no_export" if exact_sensitive_released

obligation_set contains "alert" if bg_required

obligation_set contains "review_required" if bg_required

obligations := sort([o | some o in obligation_set])

# ---------------------------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------------------------

read_like if action in {"read", "export"}

decision := {
	"allow": false,
	"policy_version": catalog.version,
	"release": {a: "deny" | some a in attributes},
	"obligations": ["log"],
	"reason_codes": [gate],
	"attribute_reasons": {a: "GATE_DENIED" | some a in attributes},
} if {
	gate
}

decision := {
	"allow": false,
	"policy_version": catalog.version,
	"release": release,
	"obligations": ["log"],
	"reason_codes": ["NOTHING_RELEASABLE"],
	"attribute_reasons": attribute_reasons,
} if {
	not gate
	read_like
	count(released) == 0
}

decision := {
	"allow": true,
	"policy_version": catalog.version,
	"release": release,
	"obligations": obligations,
	"reason_codes": ["ALLOW"],
	"attribute_reasons": attribute_reasons,
} if {
	not gate
	allowed_by_release
}

allowed_by_release if not read_like

allowed_by_release if count(released) > 0
