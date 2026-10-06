from __future__ import annotations

from typing import Any, Protocol


class SourceAdapter(Protocol):
    """Contract every authoritative-source adapter implements."""

    source: str

    def query(self, check: str, identifier: str, params: dict[str, Any]) -> dict[str, Any]:
        """Answer ``check`` for ``identifier``. Return ``{"status": "no_record"}`` when unknown."""
        ...


def schema_filter(payload: dict[str, Any], response_schema: dict[str, str]) -> dict[str, Any]:
    """Keep only the keys and types the sharing matrix allows for this check."""
    out: dict[str, Any] = {}
    for key, kind in response_schema.items():
        if key not in payload:
            continue
        value = payload[key]
        if kind == "boolean" and isinstance(value, bool):
            out[key] = value
        elif kind == "string" and isinstance(value, str):
            out[key] = value
        elif kind == "number" and isinstance(value, int | float) and not isinstance(value, bool):
            out[key] = value
    return out


def adapter_for(name: str) -> SourceAdapter:
    if name == "mock_tax":
        from .mock_sources import MockTaxAuthority

        return MockTaxAuthority()
    if name == "mock_disability":
        from .mock_sources import MockDisabilityRegistry

        return MockDisabilityRegistry()
    if name == "mock_civil":
        from .mock_sources import MockCivilRegistry

        return MockCivilRegistry()
    raise KeyError(f"no adapter registered for '{name}'")
