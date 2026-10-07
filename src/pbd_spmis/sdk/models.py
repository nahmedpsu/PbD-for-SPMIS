from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class PrivacyError(Exception):
    """Base class for SDK errors. Carries the service's stable error code."""

    def __init__(
        self, message: str, *, code: str = "error", status: int = 0, body: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.body = body or {}


class PolicyDeniedError(PrivacyError):
    """The control plane denied the operation. ``reason_codes`` names the gate that failed."""

    @property
    def reason_codes(self) -> list[str]:
        return list(self.body.get("reason_codes", []))

    @property
    def decision_id(self) -> str | None:
        return self.body.get("decision_id")


class UnavailableError(PrivacyError):
    """The control plane could not be reached or failed; callers must fail closed."""


@dataclass
class Decision:
    allow: bool
    decision_id: str
    policy_version: str
    release: dict[str, str] = field(default_factory=dict)
    obligations: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    attribute_reasons: dict[str, str] = field(default_factory=dict)
    engine: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Decision:
        return cls(
            allow=bool(data.get("allow")),
            decision_id=str(data.get("decision_id", "")),
            policy_version=str(data.get("policy_version", "")),
            release=dict(data.get("release", {})),
            obligations=list(data.get("obligations", [])),
            reason_codes=list(data.get("reason_codes", [])),
            attribute_reasons=dict(data.get("attribute_reasons", {})),
            engine=str(data.get("engine", "")),
            raw=data,
        )

    def released(self) -> list[str]:
        return [a for a, m in self.release.items() if m != "deny"]

    def mode(self, attribute: str) -> str:
        return self.release.get(attribute, "deny")

    def has_obligation(self, name: str) -> bool:
        return any(o == name or o.startswith(name + ":") for o in self.obligations)

    def obligation_value(self, name: str) -> str | None:
        for o in self.obligations:
            if o.startswith(name + ":"):
                return o.split(":", 1)[1]
        return None
