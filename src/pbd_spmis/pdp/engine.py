"""Embedded reference policy evaluator.

This is a pure function from (catalogue, policy input) to a decision. It is deliberately small
and readable: it is the executable specification of the authorisation model, and the Rego policy
in ``policy/rego`` must produce the same output for every vector in ``policy/conformance``.

Decision model
--------------
1. **Gates** are evaluated in order. The first failing gate denies the whole request and the
   reason code names it. Gates: authentication, purpose exists, program exists, purpose allowed
   for program, role allowed for purpose, action allowed for purpose, program assignment,
   subject relationship, case assignment, MFA, break-glass grant.
2. **Per-attribute release** is computed from the purpose's release map (with role overrides),
   then downgraded by context rules (low-trust device never receives C4), or, for break-glass
   purposes, taken from the grant's scope.
3. **Allow** is true when the gates pass and, for reads/exports, at least one attribute is
   released. Writes and deletes are allowed by gates alone.
4. **Obligations** are the purpose's obligations plus ``log`` always, ``no_export`` when any
   attribute of a no-export class is released *exactly*, and the break-glass obligations when a
   grant is used.

Reason codes are stable strings; they are part of the public contract.
"""

from __future__ import annotations

from typing import Any

# Request-level reason codes
ALLOW = "ALLOW"
AUTH_REQUIRED = "AUTH_REQUIRED"
UNKNOWN_ROLE = "UNKNOWN_ROLE"
UNKNOWN_PURPOSE = "UNKNOWN_PURPOSE"
UNKNOWN_PROGRAM = "UNKNOWN_PROGRAM"
PURPOSE_NOT_ALLOWED_FOR_PROGRAM = "PURPOSE_NOT_ALLOWED_FOR_PROGRAM"
ROLE_NOT_PERMITTED = "ROLE_NOT_PERMITTED"
ACTION_NOT_PERMITTED = "ACTION_NOT_PERMITTED"
NO_PROGRAM_ASSIGNMENT = "NO_PROGRAM_ASSIGNMENT"
NO_SUBJECT_RELATIONSHIP = "NO_SUBJECT_RELATIONSHIP"
NO_CASE_ASSIGNMENT = "NO_CASE_ASSIGNMENT"
MFA_REQUIRED = "MFA_REQUIRED"
BREAK_GLASS_REQUIRED = "BREAK_GLASS_REQUIRED"
BREAK_GLASS_INACTIVE = "BREAK_GLASS_INACTIVE"
BREAK_GLASS_NOT_HOLDER = "BREAK_GLASS_NOT_HOLDER"
BREAK_GLASS_SUBJECT_MISMATCH = "BREAK_GLASS_SUBJECT_MISMATCH"
NOTHING_RELEASABLE = "NOTHING_RELEASABLE"

# Attribute-level reason codes
RELEASED = "RELEASED"
NOT_IN_PURPOSE = "NOT_IN_PURPOSE"
UNKNOWN_ATTRIBUTE = "UNKNOWN_ATTRIBUTE"
LOW_TRUST_DEVICE = "LOW_TRUST_DEVICE"
ROLE_OVERRIDE = "ROLE_OVERRIDE"
BREAK_GLASS_RELEASE = "BREAK_GLASS_RELEASE"
OUT_OF_GRANT_SCOPE = "OUT_OF_GRANT_SCOPE"
GATE_DENIED = "GATE_DENIED"


def evaluate(catalog: dict[str, Any], inp: dict[str, Any]) -> dict[str, Any]:
    actor = inp.get("actor") or {}
    subject = inp.get("subject") or {}
    context = inp.get("context") or {}
    purpose_name = inp.get("purpose") or ""
    program_name = inp.get("program") or ""
    action = inp.get("action") or "read"
    attributes: list[str] = list(inp.get("attributes") or [])

    purposes = catalog["purposes"]
    programs = catalog["programs"]
    attrs = catalog["attributes"]
    policy = catalog["policy"]
    service_roles = set(policy.get("service_roles", []))
    human_roles = set(policy.get("human_roles", []))

    purpose = purposes.get(purpose_name)
    program = programs.get(program_name)
    role = actor.get("role", "")
    is_service = bool(actor.get("is_service")) and role in service_roles
    actor_programs = set(actor.get("programs") or [])
    bg = context.get("break_glass") or {}

    gate = _first_failing_gate(
        actor=actor,
        role=role,
        is_service=is_service,
        service_roles=service_roles,
        human_roles=human_roles,
        purpose=purpose,
        purpose_name=purpose_name,
        program=program,
        program_name=program_name,
        action=action,
        actor_programs=actor_programs,
        subject=subject,
        context=context,
        bg=bg,
    )

    if gate is not None:
        return {
            "allow": False,
            "policy_version": catalog["version"],
            "release": {a: "deny" for a in attributes},
            "obligations": ["log"],
            "reason_codes": [gate],
            "attribute_reasons": {a: GATE_DENIED for a in attributes},
        }

    assert purpose is not None
    release: dict[str, str] = {}
    attr_reasons: dict[str, str] = {}
    overrides = (purpose.get("role_overrides") or {}).get(role) or {}
    low_trust = context.get("device_trust") == "low"
    low_trust_classes = set(policy.get("low_trust_denies_classes", []))
    break_glass_purpose = bool(purpose.get("requires_break_glass"))

    for attr in attributes:
        spec = attrs.get(attr)
        if spec is None:
            release[attr] = "deny"
            attr_reasons[attr] = UNKNOWN_ATTRIBUTE
            continue
        if break_glass_purpose:
            if attr in set(bg.get("attributes") or []):
                release[attr] = "exact"
                attr_reasons[attr] = BREAK_GLASS_RELEASE
            else:
                release[attr] = "deny"
                attr_reasons[attr] = OUT_OF_GRANT_SCOPE
            continue
        if attr in overrides:
            mode, reason = overrides[attr], ROLE_OVERRIDE
        else:
            mode = (purpose.get("release") or {}).get(attr, "deny")
            reason = RELEASED if mode != "deny" else NOT_IN_PURPOSE
        if mode != "deny" and low_trust and spec["class"] in low_trust_classes:
            mode, reason = "deny", LOW_TRUST_DEVICE
        release[attr] = mode
        attr_reasons[attr] = reason if mode != "deny" else (reason if reason != RELEASED else NOT_IN_PURPOSE)

    released = [a for a, m in release.items() if m != "deny"]
    if action in ("read", "export") and not released:
        return {
            "allow": False,
            "policy_version": catalog["version"],
            "release": release,
            "obligations": ["log"],
            "reason_codes": [NOTHING_RELEASABLE],
            "attribute_reasons": attr_reasons,
        }

    obligations = set(purpose.get("obligations") or [])
    obligations.add("log")
    no_export_classes = set(policy.get("no_export_classes", []))
    # An *exact* release of a no-export class attribute must not leave the system. Bands,
    # assertions and tokens are already minimised and do not trigger the obligation on their own.
    if any(release[a] == "exact" and attrs[a]["class"] in no_export_classes for a in released if a in attrs):
        obligations.add("no_export")
    if break_glass_purpose:
        obligations.update({"alert", "review_required"})

    return {
        "allow": True,
        "policy_version": catalog["version"],
        "release": release,
        "obligations": sorted(obligations),
        "reason_codes": [ALLOW],
        "attribute_reasons": attr_reasons,
    }


def _first_failing_gate(
    *,
    actor: dict[str, Any],
    role: str,
    is_service: bool,
    service_roles: set[str],
    human_roles: set[str],
    purpose: dict[str, Any] | None,
    purpose_name: str,
    program: dict[str, Any] | None,
    program_name: str,
    action: str,
    actor_programs: set[str],
    subject: dict[str, Any],
    context: dict[str, Any],
    bg: dict[str, Any],
) -> str | None:
    if not actor.get("id"):
        return AUTH_REQUIRED
    if role not in service_roles and role not in human_roles:
        return UNKNOWN_ROLE
    if purpose is None:
        return UNKNOWN_PURPOSE
    if program is None:
        return UNKNOWN_PROGRAM
    if purpose_name not in set(program.get("purposes") or []):
        return PURPOSE_NOT_ALLOWED_FOR_PROGRAM
    if role not in set(purpose.get("allowed_roles") or []):
        return ROLE_NOT_PERMITTED
    if action not in set(purpose.get("actions") or []):
        return ACTION_NOT_PERMITTED
    wildcard = is_service and "*" in actor_programs
    if not wildcard and program_name not in actor_programs:
        return NO_PROGRAM_ASSIGNMENT
    if purpose.get("requires_relationship"):
        if program_name not in set(subject.get("program_relationship") or []):
            return NO_SUBJECT_RELATIONSHIP
    if purpose.get("requires_case_assignment") and not is_service:
        case_id = context.get("case_id")
        if not case_id or case_id not in set(actor.get("cases") or []):
            return NO_CASE_ASSIGNMENT
    if purpose.get("requires_mfa") and not is_service and "mfa" not in set(actor.get("amr") or []):
        return MFA_REQUIRED
    if purpose.get("requires_break_glass"):
        if not bg:
            return BREAK_GLASS_REQUIRED
        if not bg.get("active"):
            return BREAK_GLASS_INACTIVE
        if bg.get("actor_id") != actor.get("id"):
            return BREAK_GLASS_NOT_HOLDER
        if not bg.get("matches_subject"):
            return BREAK_GLASS_SUBJECT_MISMATCH
    return None
