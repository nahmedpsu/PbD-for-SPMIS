"""Policy Enforcement Point (PEP) library.

Every sensitive service embeds this PEP. It asks the Policy Decision Point for a decision,
enforces allow/deny, applies the per-attribute *release transformation* the decision prescribes
(exact, band, assertion, precision reduction, token, deny) and attaches the obligations to the
response. The transformations live here, next to the data, because only the data-holding
service can turn a raw value into an assertion.

The PEP fails closed: if the PDP is unreachable, sensitive reads are refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Response

from ..catalog.loader import Catalog, get_catalog
from ..sdk.transforms import transform as sdk_transform
from .audit_client import emit
from .clients import client
from .context import RequestContext
from .errors import PolicyDenied, UpstreamUnavailable


@dataclass
class Decision:
    allow: bool
    decision_id: str
    policy_version: str
    release: dict[str, str] = field(default_factory=dict)
    obligations: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    attribute_reasons: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def released(self) -> list[str]:
        return [a for a, r in self.release.items() if r != "deny"]


def decide(
    caller: str,
    ctx: RequestContext,
    *,
    subject_token: str | None,
    subject_programs: list[str] | None,
    attributes: list[str],
    action: str = "read",
    purpose: str | None = None,
    program: str | None = None,
) -> Decision:
    """Obtain a decision from the PDP, including break-glass context if a grant is referenced."""
    policy_ctx = ctx.policy_context()
    if ctx.break_glass_grant:
        policy_ctx["break_glass"] = _load_grant(caller, ctx, subject_token)
    payload = {
        "actor": ctx.actor.to_policy_input(),
        "subject": {"person_token": subject_token, "program_relationship": subject_programs or []},
        "program": program or ctx.program,
        "purpose": purpose or ctx.purpose,
        "action": action,
        "attributes": attributes,
        "context": policy_ctx,
    }
    resp = client("pdp", caller=caller).post(
        "/v1/decisions", json=payload, headers={"X-Correlation-ID": ctx.correlation_id}
    )
    if resp.status_code != 200:
        raise UpstreamUnavailable(f"policy decision point returned {resp.status_code}")
    data = resp.json()
    return Decision(
        allow=bool(data["allow"]),
        decision_id=data["decision_id"],
        policy_version=data.get("policy_version", ""),
        release=data.get("release", {}),
        obligations=data.get("obligations", []),
        reason_codes=data.get("reason_codes", []),
        attribute_reasons=data.get("attribute_reasons", {}),
        raw=data,
    )


def enforce(
    caller: str,
    ctx: RequestContext,
    *,
    subject_token: str | None,
    subject_programs: list[str] | None,
    attributes: list[str],
    action: str = "read",
    purpose: str | None = None,
    program: str | None = None,
) -> Decision:
    """Decide and raise :class:`PolicyDenied` when the decision is deny. Audits the outcome."""
    decision = decide(
        caller,
        ctx,
        subject_token=subject_token,
        subject_programs=subject_programs,
        attributes=attributes,
        action=action,
        purpose=purpose,
        program=program,
    )
    emit(
        caller,
        ctx,
        "access_decision",
        outcome="allow" if decision.allow else "deny",
        subject_token=subject_token,
        attributes=attributes,
        decision_id=decision.decision_id,
        reason_codes=decision.reason_codes,
        obligations=decision.obligations,
        details={"action": action, "release": decision.release},
        purpose=purpose,
        program=program,
    )
    if not decision.allow:
        raise PolicyDenied(
            "access denied by policy",
            extra={"decision_id": decision.decision_id, "reason_codes": decision.reason_codes},
        )
    return decision


def _load_grant(caller: str, ctx: RequestContext, subject_token: str | None) -> dict[str, Any]:
    resp = client("breakglass", caller=caller).get(
        f"/v1/grants/{ctx.break_glass_grant}", headers={"X-Correlation-ID": ctx.correlation_id}
    )
    if resp.status_code != 200:
        return {"active": False}
    grant = resp.json()
    return {
        "active": grant.get("status") == "active",
        "grant_id": grant.get("grant_id"),
        "attributes": grant.get("attributes", []),
        "subject_token": grant.get("subject_token"),
        "actor_id": grant.get("requester_id"),
        "matches_subject": grant.get("subject_token") == subject_token,
    }


# --------------------------------------------------------------------------------------------
# Release transformations
# --------------------------------------------------------------------------------------------


def apply_release(
    record: dict[str, Any],
    decision: Decision,
    *,
    program: str,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    """Transform a raw record into exactly what the decision permits."""
    cat = catalog or get_catalog()
    out: dict[str, Any] = {}
    for attr, mode in decision.release.items():
        if mode == "deny" or attr not in record:
            continue
        out[attr] = transform(attr, record[attr], mode, program=program, catalog=cat)
    return out


def transform(attr: str, value: Any, mode: str, *, program: str, catalog: Catalog) -> Any:
    """Apply one release mode. The implementation is shared with the SDK and the adapters."""
    try:
        return sdk_transform(attr, value, mode, program=program, catalog=catalog.view())
    except ValueError as exc:
        raise PolicyDenied(str(exc), code="unknown_release_mode") from exc


def attach_obligations(response: Response, decision: Decision) -> None:
    response.headers["X-Decision-ID"] = decision.decision_id
    response.headers["X-Policy-Version"] = decision.policy_version
    if decision.obligations:
        response.headers["X-Obligations"] = ",".join(decision.obligations)
    if "no_export" in decision.obligations:
        response.headers["Cache-Control"] = "no-store"
