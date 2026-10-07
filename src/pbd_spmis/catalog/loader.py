"""Load, validate and query the privacy catalogue.

The catalogue is a set of YAML files owned by privacy administrators. It is validated against a
JSON Schema and a set of cross-reference rules so that a policy can never reference an attribute
or purpose that does not exist. The same merged document is exported as the OPA data bundle, so
the embedded engine and the Rego policy evaluate identical facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from ..config import get_settings

FILES = {
    "attributes": "attributes.yaml",
    "purposes": "purposes.yaml",
    "programs": "programs.yaml",
    "policy": "policy.yaml",
    "retention": "retention.yaml",
    "sharing": "sharing.yaml",
}


class CatalogError(ValueError):
    pass


@dataclass
class Catalog:
    data: dict[str, Any]
    source_dir: Path

    # ------------------------------------------------------------------ basic accessors
    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def attributes(self) -> dict[str, Any]:
        return self.data["attributes"]

    @property
    def purposes(self) -> dict[str, Any]:
        return self.data["purposes"]

    @property
    def programs(self) -> dict[str, Any]:
        return self.data["programs"]

    @property
    def policy(self) -> dict[str, Any]:
        return self.data["policy"]

    @property
    def retention(self) -> dict[str, Any]:
        return self.data["retention"]

    @property
    def sharing(self) -> dict[str, Any]:
        return self.data["sharing"]

    def attribute_class(self, attr: str) -> str | None:
        a = self.attributes.get(attr)
        return a["class"] if a else None

    def program_code(self, program: str) -> str:
        return self.programs[program]["code"]

    def program_by_code(self, code: str) -> str | None:
        for name, p in self.programs.items():
            if p["code"] == code.upper():
                return name
        return None

    # ------------------------------------------------------------------ transformations
    def band_for(self, attr: str, value: Any) -> str | None:
        bands = self.attributes.get(attr, {}).get("bands") or []
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        for band in bands:
            if band["max"] is None or v < band["max"]:
                return band["label"]
        return bands[-1]["label"] if bands else None

    def assert_for(self, attr: str, assertion: str, value: Any, *, program: str) -> bool | None:
        prog = self.programs.get(program, {})
        if attr == "income" and assertion == "below_threshold":
            threshold = prog.get("income_threshold")
            if threshold is None or value is None:
                return None
            try:
                return float(value) < float(threshold)
            except (TypeError, ValueError):
                return None
        if attr == "disability_status" and assertion == "eligible":
            return value in set(prog.get("eligible_disability_statuses", []))
        return None

    # ------------------------------------------------------------------ retention
    def retention_rule(self, record_type: str, data_class: str | None = None) -> dict[str, Any]:
        rule = self.retention["records"].get(record_type)
        if rule:
            return rule
        return self.retention["defaults"].get(
            data_class or "C3", {"period_days": 365, "end_action": "review"}
        )

    def view(self) -> dict[str, Any]:
        """Non-sensitive catalogue view for clients: classes, bands, thresholds, release vocab."""
        return {
            "version": self.version,
            "attributes": {
                name: {
                    "class": a["class"],
                    "category": a.get("category"),
                    "disclosures": list(a.get("disclosures", [])),
                    "bands": a.get("bands", []),
                }
                for name, a in self.attributes.items()
            },
            "programs": {
                name: {
                    "code": p["code"],
                    "name": p.get("name"),
                    "income_threshold": p.get("income_threshold"),
                    "eligible_disability_statuses": list(p.get("eligible_disability_statuses", [])),
                    "purposes": list(p.get("purposes", [])),
                }
                for name, p in self.programs.items()
            },
            "purposes": {
                name: {
                    "allowed_roles": list(p.get("allowed_roles", [])),
                    "actions": list(p.get("actions", [])),
                }
                for name, p in self.purposes.items()
            },
            "roles": {
                "service": list(self.policy.get("service_roles", [])),
                "human": list(self.policy.get("human_roles", [])),
            },
        }

    # ------------------------------------------------------------------ export
    def to_bundle(self) -> dict[str, Any]:
        """The catalogue as OPA ``data.catalog``."""
        return json.loads(json.dumps(self.data))


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise CatalogError(f"{path.name}: expected a mapping at top level")
    return doc


def merge_catalog_files(directory: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    versions: set[str] = set()
    for key, filename in FILES.items():
        path = directory / filename
        if not path.exists():
            raise CatalogError(f"missing catalogue file: {path}")
        doc = _read_yaml(path)
        versions.add(str(doc.get("version", "")))
        if key not in doc:
            # purposes.yaml also carries lawful_bases; sharing.yaml carries sources+checks.
            if key == "sharing":
                merged["sharing"] = {"sources": doc.get("sources", {}), "checks": doc.get("checks", {})}
                continue
            raise CatalogError(f"{filename}: expected top-level key '{key}'")
        merged[key] = doc[key]
        if key == "purposes":
            merged["lawful_bases"] = doc.get("lawful_bases", {})
    if len(versions) != 1:
        raise CatalogError(f"catalogue files disagree on version: {sorted(versions)}")
    merged["version"] = versions.pop()
    return merged


def validate_catalog(data: dict[str, Any], schema_path: Path | None = None) -> list[str]:
    """Return a list of problems. Empty list means valid."""
    problems: list[str] = []
    schema_file = schema_path or (
        Path(__file__).resolve().parents[3] / "catalog" / "schemas" / "catalog.schema.json"
    )
    with schema_file.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        problems.append(f"schema: {loc}: {err.message}")

    attrs = data.get("attributes", {})
    purposes = data.get("purposes", {})
    programs = data.get("programs", {})
    lawful = data.get("lawful_bases", {})
    policy = data.get("policy", {})
    known_roles = set(policy.get("service_roles", [])) | set(policy.get("human_roles", []))

    for pname, p in purposes.items():
        if lawful and p.get("lawful_basis") not in lawful:
            problems.append(f"purpose {pname}: unknown lawful_basis '{p.get('lawful_basis')}'")
        for role in p.get("allowed_roles", []):
            if known_roles and role not in known_roles:
                problems.append(f"purpose {pname}: unknown role '{role}'")
        for attr, mode in p.get("release", {}).items():
            if attr not in attrs:
                problems.append(f"purpose {pname}: release references unknown attribute '{attr}'")
            elif mode not in attrs[attr].get("disclosures", []):
                problems.append(f"purpose {pname}: mode '{mode}' is not a permitted disclosure for '{attr}'")
        for role, overrides in p.get("role_overrides", {}).items():
            if role not in p.get("allowed_roles", []):
                problems.append(f"purpose {pname}: role_override for role '{role}' not in allowed_roles")
            for attr, mode in overrides.items():
                if attr not in attrs:
                    problems.append(f"purpose {pname}: role_override references unknown attribute '{attr}'")
                elif mode not in attrs[attr].get("disclosures", []):
                    problems.append(f"purpose {pname}: override mode '{mode}' not permitted for '{attr}'")
        if p.get("requires_break_glass") and p.get("release"):
            problems.append(f"purpose {pname}: break-glass purposes must not pre-register releases")
        if "export" in p.get("actions", []) and any(
            attrs.get(a, {}).get("class") == "C4" and m == "exact" for a, m in p.get("release", {}).items()
        ):
            problems.append(f"purpose {pname}: export purposes must not release C4 attributes exactly")

    for prname, pr in programs.items():
        for pu in pr.get("purposes", []):
            if pu not in purposes:
                problems.append(f"program {prname}: unknown purpose '{pu}'")
        checks = set(data.get("sharing", {}).get("checks", {}))
        for rule in pr.get("eligibility", {}).get("all_of", []) + pr.get("eligibility", {}).get("any_of", []):
            if checks and rule not in checks:
                problems.append(f"program {prname}: eligibility rule '{rule}' is not a known sharing check")

    for src, s in data.get("sharing", {}).get("sources", {}).items():
        for cname, c in s.get("checks", {}).items():
            for pu in c.get("purposes", []):
                if pu not in purposes:
                    problems.append(f"sharing {src}.{cname}: unknown purpose '{pu}'")
            if c.get("identifier") not in attrs:
                problems.append(
                    f"sharing {src}.{cname}: unknown identifier attribute '{c.get('identifier')}'"
                )
    for cname, attr in data.get("sharing", {}).get("checks", {}).items():
        if attr not in attrs:
            problems.append(f"sharing check {cname}: unknown attribute '{attr}'")
    return problems


def load_catalog(directory: Path | None = None, *, validate: bool = True) -> Catalog:
    directory = directory or get_settings().catalog_dir
    data = merge_catalog_files(directory)
    if validate:
        problems = validate_catalog(data)
        if problems:
            raise CatalogError("catalogue is invalid:\n  " + "\n  ".join(problems))
    return Catalog(data=data, source_dir=directory)


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = load_catalog()
    return _catalog


def reset_catalog() -> None:
    global _catalog
    _catalog = None
