"""Module configuration.

openIMIS modules read their settings from ``ModuleConfiguration`` (database) with a
``DEFAULT_CFG`` fallback. This module does the same, and also accepts environment variables so it
can run in tests and containers without a database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pbd_spmis.sdk import PrivacyControlPlane
from pbd_spmis.sdk.mapping import Mapping

DEFAULT_MAPPING = Path(__file__).resolve().parents[2] / "mapping.yaml"

DEFAULT_CFG: dict[str, Any] = {
    # Base URL of the PbD-SPMIS control plane (all-in-one) or per-service URLs.
    "control_plane_url": os.getenv("PBD_CONTROL_PLANE_URL", "http://localhost:8000"),
    "service_urls": {},
    # Service token for this openIMIS instance (role OPENIMIS... mapped to REGISTRY_SERVICE).
    "service_token": os.getenv("PBD_SERVICE_TOKEN", ""),
    "mapping_file": os.getenv("PBD_MAPPING_FILE", str(DEFAULT_MAPPING)),
    # Deny mapped reads that arrive without X-Purpose instead of using the operation default.
    "require_purpose_header": False,
    # Program relationship for subjects: "assume" (subject related to the request's program) or
    # "resolver" (call the configured python path, e.g. pbd.relationships.from_beneficiaries).
    "relationship_mode": "assume",
    "relationship_resolver": "",
    # Fail closed when the control plane is unavailable (recommended).
    "fail_closed": True,
    # Emit a read-access audit event per entity per request.
    "audit_reads": True,
    # Move identifiers to the vault on create/update mutations (opt-in; changes stored data).
    "vault_identifiers": False,
    # Agency name reported for actors.
    "agency": "SOCIAL_PROTECTION_AGENCY",
    # Value returned for denied attributes whose GraphQL field is non-nullable (openIMIS declares
    # firstName/lastName as String!).
    "redaction_marker": "***",
}


@dataclass
class PbdConfig:
    control_plane_url: str
    service_urls: dict[str, str]
    service_token: str
    mapping_file: str
    require_purpose_header: bool
    relationship_mode: str
    relationship_resolver: str
    fail_closed: bool
    audit_reads: bool
    vault_identifiers: bool
    agency: str
    redaction_marker: str = "***"
    mapping: Mapping = field(default=None)  # type: ignore[assignment]
    control_plane: PrivacyControlPlane = field(default=None)  # type: ignore[assignment]

    @classmethod
    def from_dict(
        cls,
        cfg: dict[str, Any],
        *,
        control_plane: PrivacyControlPlane | None = None,
        mapping: Mapping | None = None,
    ) -> PbdConfig:
        merged = {**DEFAULT_CFG, **(cfg or {})}
        c = cls(
            control_plane_url=str(merged["control_plane_url"]),
            service_urls=dict(merged.get("service_urls") or {}),
            service_token=str(merged.get("service_token") or ""),
            mapping_file=str(merged["mapping_file"]),
            require_purpose_header=bool(merged["require_purpose_header"]),
            relationship_mode=str(merged["relationship_mode"]),
            relationship_resolver=str(merged.get("relationship_resolver") or ""),
            fail_closed=bool(merged["fail_closed"]),
            audit_reads=bool(merged["audit_reads"]),
            vault_identifiers=bool(merged["vault_identifiers"]),
            agency=str(merged["agency"]),
            redaction_marker=str(merged.get("redaction_marker", "***")),
        )
        c.mapping = mapping or Mapping.load(c.mapping_file)
        c.control_plane = control_plane or PrivacyControlPlane(
            c.control_plane_url, token=c.service_token or None, service_urls=c.service_urls or None
        )
        return c


_current: PbdConfig | None = None


def configure(
    cfg: dict[str, Any] | None = None,
    *,
    control_plane: PrivacyControlPlane | None = None,
    mapping: Mapping | None = None,
) -> PbdConfig:
    """Set the active configuration (called by the AppConfig, tests and management commands)."""
    global _current
    _current = PbdConfig.from_dict(cfg or {}, control_plane=control_plane, mapping=mapping)
    return _current


def current() -> PbdConfig:
    global _current
    if _current is None:
        _current = PbdConfig.from_dict({})
    return _current
