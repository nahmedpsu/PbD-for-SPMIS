"""Entity/field/role/purpose mapping used by adapters (openIMIS module, gateway).

The mapping file format is documented in ``integrations/openimis/mapping.yaml``. This module is
product-neutral: any system that exposes typed objects with fields can be mapped the same way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake(name: str) -> str:
    return _CAMEL.sub("_", name).lower()


def get_path(obj: Any, path: str) -> Any:
    """Read ``a.b.c`` from dicts or objects, tolerating camelCase/snake_case either way."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            elif to_snake(part) in cur:
                cur = cur[to_snake(part)]
            else:
                return None
        else:
            cur = getattr(cur, part, getattr(cur, to_snake(part), None))
    return cur


def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        key = part if part in cur or to_snake(part) not in cur else to_snake(part)
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    last = parts[-1]
    key = last if last in cur or to_snake(last) not in cur else to_snake(last)
    cur[key] = value


@dataclass
class EntityMapping:
    name: str
    gql_types: list[str] = field(default_factory=list)
    rest_prefixes: list[str] = field(default_factory=list)
    subject_token_paths: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)  # field path -> attribute
    attributes: list[str] = field(default_factory=list)

    def attribute_for(self, field_path: str) -> str | None:
        if field_path in self.fields:
            return self.fields[field_path]
        # tolerate snake_case field names (Django model attributes) for camelCase mapping keys
        for k, v in self.fields.items():
            if to_snake(k) == to_snake(field_path):
                return v
        return None

    def sub_fields(self, prefix: str) -> dict[str, str]:
        """Mapping entries under a container field such as ``jsonExt``: key -> attribute."""
        out: dict[str, str] = {}
        for k, v in self.fields.items():
            head, _, rest = k.partition(".")
            if rest and to_snake(head) == to_snake(prefix):
                out[rest] = v
        return out

    def subject_token(self, obj: Any) -> str | None:
        for p in self.subject_token_paths:
            v = get_path(obj, p)
            if v:
                return str(v)
        return None


@dataclass
class RoleRule:
    role: str
    any_rights: list[int] = field(default_factory=list)
    all_rights: list[int] = field(default_factory=list)
    none_of: list[int] = field(default_factory=list)

    def matches(self, rights: set[int]) -> bool:
        if self.none_of and rights & set(self.none_of):
            return False
        if self.all_rights and not set(self.all_rights) <= rights:
            return False
        if self.any_rights and not rights & set(self.any_rights):
            return False
        return bool(self.any_rights or self.all_rights)


@dataclass
class Mapping:
    entities: dict[str, EntityMapping]
    programs_by_code: dict[str, str]
    default_program: str
    roles: list[RoleRule]
    operations: dict[str, dict[str, Any]]
    vault: dict[str, Any]
    _by_type: dict[str, EntityMapping] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for e in self.entities.values():
            for t in e.gql_types:
                self._by_type[t] = e

    @classmethod
    def load(cls, path: str | Path) -> Mapping:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mapping:
        entities = {
            name: EntityMapping(
                name=name,
                gql_types=list(spec.get("gql_types", [])),
                rest_prefixes=list(spec.get("rest_prefixes", [])),
                subject_token_paths=list(spec.get("subject_token_paths", [])),
                fields=dict(spec.get("fields", {})),
                attributes=list(spec.get("attributes", [])),
            )
            for name, spec in (data.get("entities") or {}).items()
        }
        programs = data.get("programs") or {}
        roles = [
            RoleRule(
                role=r["role"],
                any_rights=[int(x) for x in r.get("any_rights", [])],
                all_rights=[int(x) for x in r.get("all_rights", [])],
                none_of=[int(x) for x in r.get("none_of", [])],
            )
            for r in data.get("roles") or []
        ]
        return cls(
            entities=entities,
            programs_by_code={
                str(k).upper(): v for k, v in (programs.get("by_benefit_plan_code") or {}).items()
            },
            default_program=str(programs.get("default", "")),
            roles=roles,
            operations=dict(data.get("operations") or {}),
            vault=dict(data.get("vault") or {}),
        )

    # ------------------------------------------------------------------ lookups
    def entity_for_type(self, gql_type: str) -> EntityMapping | None:
        return self._by_type.get(gql_type)

    def entity_for_rest_path(self, path: str) -> EntityMapping | None:
        for e in self.entities.values():
            if any(path.startswith(p) for p in e.rest_prefixes):
                return e
        return None

    def role_for_rights(self, rights: Iterable[int]) -> str | None:
        rs = {int(r) for r in rights}
        for rule in self.roles:
            if rule.matches(rs):
                return rule.role
        return None

    def program_for_code(self, code: str | None) -> str:
        if code and code.upper() in self.programs_by_code:
            return self.programs_by_code[code.upper()]
        return self.default_program

    def operation(self, name: str) -> dict[str, Any] | None:
        return self.operations.get(name)
