"""PbD-SPMIS client SDK.

A dependency-light client for the Privacy Control Plane API (v1) that any MIS, adapter or
gateway can embed. It depends only on ``httpx`` and never imports the service packages, so it
can be vendored or installed on its own.

    from pbd_spmis.sdk import PrivacyControlPlane

    cp = PrivacyControlPlane("https://privacy.example.gov", token=lambda: my_service_token())
    decision = cp.pdp.decide(actor=..., subject=..., program="cash_assistance",
                             purpose="eligibility_verification", attributes=["income", "address"])
    view = cp.apply_release(record, decision, program="cash_assistance")
    cp.audit.emit("access_decision", actor_id=..., outcome="allow", decision_id=decision.decision_id)
"""

from .client import AuditClient, BreakGlassClient, BrokerClient, PdpClient, PrivacyControlPlane, VaultClient
from .models import Decision, PolicyDeniedError, PrivacyError, UnavailableError
from .transforms import apply_release, transform

__all__ = [
    "AuditClient",
    "BreakGlassClient",
    "BrokerClient",
    "Decision",
    "PdpClient",
    "PolicyDeniedError",
    "PrivacyControlPlane",
    "PrivacyError",
    "UnavailableError",
    "VaultClient",
    "apply_release",
    "transform",
]
