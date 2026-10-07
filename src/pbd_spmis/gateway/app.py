"""Privacy gateway.

A reverse proxy for systems that cannot embed the control plane (legacy CORE-MIS, third-party
MIS, vendor APIs). It understands two API styles:

* **GraphQL** (``graphql_path``): the operation document is parsed, each mapped top-level field
  is bound to an entity, one decision per entity is obtained, the upstream response is rewritten
  so that mapped fields carry only what the decision releases, and a ``read_access`` event is
  recorded. Unmapped operations pass through untouched.
* **REST** (``rest_prefixes`` from the mapping, e.g. FHIR ``/Patient``): the JSON body (single
  resource or bundle) is filtered the same way.

Actor identity comes from the client's bearer token (``actor.source: jwt`` with the upstream's
signing key and claim names) or from headers set by an identity-aware proxy in front of the
gateway (``actor.source: header``: ``X-Actor-Id``, ``X-Actor-Role``, ``X-Actor-Programs``,
``X-Actor-Rights``). Purpose is required (``X-Purpose``) unless the mapping defines a default
for the operation.

Limitations: the gateway sees responses, so it cannot vault data at rest and cannot see
internal jobs; and response rewriting relies on the mapping's result paths. Use the openIMIS
module where possible and the gateway where it is the only option.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from graphql import FieldNode, OperationDefinitionNode, parse
from starlette.concurrency import run_in_threadpool

from ..sdk import Decision, PolicyDeniedError, PrivacyControlPlane, UnavailableError, transform
from ..sdk.mapping import EntityMapping, Mapping, get_path, to_snake

RESULT_PATHS_DEFAULT = ["edges.*.node", "*", ""]


@dataclass
class GatewayConfig:
    upstream: str
    mapping: Mapping
    control_plane: PrivacyControlPlane
    graphql_path: str = "/api/graphql"
    actor_source: str = "header"  # "header" or "jwt"
    jwt_secret: str = ""
    jwt_claims: dict[str, str] = field(
        default_factory=lambda: {
            "id": "username",
            "role": "pbd_role",
            "rights": "rights",
            "programs": "programs",
        }
    )
    static_roles: dict[str, str] = field(default_factory=dict)  # actor id -> role (pilots)
    agency: str = "SOCIAL_PROTECTION_AGENCY"
    fail_closed: bool = True
    result_paths: dict[str, list[str]] = field(default_factory=dict)  # operation -> candidate paths

    @classmethod
    def from_file(
        cls, path: str | Path, *, control_plane: PrivacyControlPlane | None = None
    ) -> GatewayConfig:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        cp_cfg = data.get("control_plane") or {}
        cp = control_plane or PrivacyControlPlane(
            cp_cfg.get("base_url"), token=cp_cfg.get("token"), service_urls=cp_cfg.get("service_urls")
        )
        mapping_path = Path(data["mapping"])
        if not mapping_path.is_absolute():
            mapping_path = Path(path).resolve().parent / mapping_path
        actor = data.get("actor") or {}
        return cls(
            upstream=str(data["upstream"]).rstrip("/"),
            mapping=Mapping.load(mapping_path),
            control_plane=cp,
            graphql_path=str(data.get("graphql_path", "/api/graphql")),
            actor_source=str(actor.get("source", "header")),
            jwt_secret=str(actor.get("jwt_secret", "")),
            jwt_claims={
                **{"id": "username", "role": "pbd_role", "rights": "rights", "programs": "programs"},
                **(actor.get("jwt_claims") or {}),
            },
            static_roles=dict(actor.get("static_roles") or {}),
            agency=str(data.get("agency", cls.agency)),
            fail_closed=bool(data.get("fail_closed", True)),
            result_paths=dict(data.get("result_paths") or {}),
        )


def create_gateway(cfg: GatewayConfig, *, upstream_transport: httpx.BaseTransport | None = None) -> FastAPI:
    app = FastAPI(title="PbD-SPMIS Privacy Gateway", version="0.2.2")
    upstream = httpx.Client(base_url=cfg.upstream, transport=upstream_transport, timeout=30.0)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "upstream": cfg.upstream}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request) -> Response:
        body = await request.body()
        full = "/" + path
        # The proxy logic is synchronous (httpx.Client); run it off the event loop.
        if full == cfg.graphql_path and request.method == "POST":
            return await run_in_threadpool(_handle_graphql, cfg, upstream, request, body)
        entity = cfg.mapping.entity_for_rest_path(full)
        if entity is not None and request.method == "GET":
            return await run_in_threadpool(_handle_rest, cfg, upstream, request, full, entity)
        return await run_in_threadpool(_forward, upstream, request, full, body)

    return app


# ------------------------------------------------------------------------------------------
# plumbing
# ------------------------------------------------------------------------------------------

HOP = {"host", "content-length", "transfer-encoding", "connection"}


def _forward(upstream: httpx.Client, request: Request, path: str, body: bytes) -> Response:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    r = upstream.request(
        request.method, path, params=dict(request.query_params), headers=headers, content=body
    )
    return Response(
        content=r.content,
        status_code=r.status_code,
        headers={k: v for k, v in r.headers.items() if k.lower() not in HOP},
    )


def _json_error(status: int, code: str, detail: str, **extra: Any) -> Response:
    return Response(
        content=json.dumps({"error": code, "detail": detail, **extra}),
        status_code=status,
        media_type="application/json",
    )


def _actor(cfg: GatewayConfig, request: Request, programs: list[str]) -> dict[str, Any] | None:
    if cfg.actor_source == "jwt":
        claims = _decode_jwt(request.headers.get("authorization", ""), cfg.jwt_secret)
        if claims is None:
            return None
        aid = str(claims.get(cfg.jwt_claims["id"], "") or claims.get("sub", ""))
        role = claims.get(cfg.jwt_claims["role"]) or cfg.static_roles.get(aid)
        if role is None:
            rights = claims.get(cfg.jwt_claims["rights"]) or []
            role = cfg.mapping.role_for_rights(rights)
        progs = claims.get(cfg.jwt_claims["programs"]) or programs
        amr = claims.get("amr") or ["pwd"]
    else:
        aid = request.headers.get("x-actor-id", "")
        role = request.headers.get("x-actor-role") or cfg.static_roles.get(aid)
        if role is None:
            rights = [r for r in request.headers.get("x-actor-rights", "").split(",") if r.strip()]
            role = cfg.mapping.role_for_rights([int(r) for r in rights if r.strip().isdigit()])
        progs = [p for p in request.headers.get("x-actor-programs", "").split(",") if p] or programs
        amr = ["pwd", "mfa"] if request.headers.get("x-actor-mfa") == "true" else ["pwd"]
    if not aid or not role:
        return None
    return {
        "id": aid,
        "role": role,
        "agency": cfg.agency,
        "programs": list(progs),
        "cases": [],
        "amr": amr,
        "is_service": False,
    }


def _decode_jwt(auth: str, secret: str) -> dict[str, Any] | None:
    if not auth.lower().startswith("bearer "):
        return None
    try:
        h, p, s = auth[7:].strip().split(".")
    except ValueError:
        return None
    if secret:
        expected = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))):
            return None
    try:
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except ValueError:
        return None


def _context(request: Request, correlation_id: str) -> dict[str, Any]:
    return {
        "channel": request.headers.get("x-channel", "gateway"),
        "device_trust": request.headers.get("x-device-trust", "managed"),
        "case_id": request.headers.get("x-case-id"),
        "correlation_id": correlation_id,
    }


def _decide(
    cfg: GatewayConfig,
    request: Request,
    entity: EntityMapping,
    purpose: str,
    program: str,
    action: str,
    correlation_id: str,
) -> Decision | Response:
    actor = _actor(cfg, request, [program])
    if actor is None:
        return _json_error(401, "actor_unresolved", "no actor identity or PbD role could be derived")
    try:
        return cfg.control_plane.pdp.decide(
            actor=actor,
            program=program,
            purpose=purpose,
            action=action,
            attributes=list(entity.attributes),
            subject_token=None,
            subject_programs=[program],
            context=_context(request, correlation_id),
            correlation_id=correlation_id,
        )
    except PolicyDeniedError as exc:
        return _json_error(403, "policy_denied", "denied by policy", reason_codes=exc.reason_codes)
    except UnavailableError as exc:
        return _json_error(503, "control_plane_unavailable", str(exc))


def _audit(
    cfg: GatewayConfig,
    request: Request,
    entity: EntityMapping,
    decision: Decision,
    purpose: str,
    program: str,
    operation: str,
    subjects: int,
    correlation_id: str,
) -> None:
    actor = _actor(cfg, request, [program]) or {"id": "unknown", "role": ""}
    try:
        cfg.control_plane.audit.emit(
            "read_access",
            service="gateway",
            actor_id=str(actor["id"]),
            actor_role=str(actor.get("role", "")),
            outcome="allow" if decision.allow else "deny",
            purpose=purpose,
            program=program,
            attributes=decision.released(),
            decision_id=decision.decision_id,
            obligations=decision.obligations,
            correlation_id=correlation_id,
            details={
                "entity": entity.name,
                "operation": operation,
                "subjects": subjects,
                "release": decision.release,
            },
        )
    except UnavailableError:
        if cfg.fail_closed:
            raise


# ------------------------------------------------------------------------------------------
# GraphQL
# ------------------------------------------------------------------------------------------


def _handle_graphql(cfg: GatewayConfig, upstream: httpx.Client, request: Request, body: bytes) -> Response:
    try:
        payload = json.loads(body or b"{}")
        document = parse(payload.get("query", ""))
    except Exception:  # noqa: BLE001 - let upstream produce the GraphQL error
        return _forward(upstream, request, cfg.graphql_path, body)

    correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    plan: list[tuple[str, str, EntityMapping, Decision, str, str, str, dict[str, str]]] = []
    for definition in document.definitions:
        if not isinstance(definition, OperationDefinitionNode):
            continue
        is_mutation = definition.operation.value == "mutation"
        for sel in definition.selection_set.selections:
            if not isinstance(sel, FieldNode):
                continue
            op_name = sel.name.value
            alias = sel.alias.value if sel.alias else op_name
            op = cfg.mapping.operation(op_name)
            if op is None:
                continue
            entity = _entity_for_operation(cfg.mapping, op_name)
            if entity is None:
                continue
            purpose = request.headers.get("x-purpose") or op.get("purpose")
            if not purpose:
                return _json_error(422, "purpose_required", f"X-Purpose is required for '{op_name}'")
            program = (
                cfg.mapping.program_for_code(request.headers.get("x-program"))
                if request.headers.get("x-program") not in cfg.mapping.programs_by_code.values()
                else request.headers["x-program"]
            )
            action = str(op.get("action") or ("write" if is_mutation else "read"))
            decision = _decide(cfg, request, entity, purpose, program, action, correlation_id)
            if isinstance(decision, Response):
                return decision
            if not decision.allow:
                return _json_error(
                    403,
                    "policy_denied",
                    "denied by policy",
                    reason_codes=decision.reason_codes,
                    decision_id=decision.decision_id,
                )
            aliases = _alias_map(sel)
            plan.append((alias, op_name, entity, decision, purpose, program, action, aliases))

    r = upstream.post(
        cfg.graphql_path,
        content=body,
        headers={k: v for k, v in request.headers.items() if k.lower() not in HOP},
    )
    if not plan or r.status_code != 200:
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")
    try:
        data = r.json()
    except ValueError:
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")

    catalog = cfg.control_plane.catalog()
    for alias, op_name, entity, decision, purpose, program, _action, aliases in plan:
        node = (data.get("data") or {}).get(alias)
        count = _rewrite_nodes(
            node,
            entity,
            decision,
            program,
            catalog,
            aliases,
            cfg.result_paths.get(op_name, RESULT_PATHS_DEFAULT),
        )
        _audit(cfg, request, entity, decision, purpose, program, op_name, count, correlation_id)
    headers = {
        "X-Correlation-ID": correlation_id,
        "X-Policy-Version": plan[0][3].policy_version,
        "X-Obligations": ",".join(sorted({o for p in plan for o in p[3].obligations})),
    }
    return Response(content=json.dumps(data), status_code=200, media_type="application/json", headers=headers)


def _entity_for_operation(mapping: Mapping, op_name: str) -> EntityMapping | None:
    base = op_name
    for suffix in ("Export", "s"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    for prefix in ("create", "update", "delete"):
        if base.startswith(prefix):
            base = base[len(prefix) :]
    for name, e in mapping.entities.items():
        if name.lower() == base.lower():
            return e
    return None


def _alias_map(field_node: FieldNode) -> dict[str, str]:
    """alias -> field name for every selection beneath the operation (any depth)."""
    out: dict[str, str] = {}

    def walk(node: FieldNode) -> None:
        if node.selection_set is None:
            return
        for s in node.selection_set.selections:
            if isinstance(s, FieldNode):
                out[s.alias.value if s.alias else s.name.value] = s.name.value
                walk(s)

    walk(field_node)
    return out


def _rewrite_nodes(
    node: Any,
    entity: EntityMapping,
    decision: Decision,
    program: str,
    catalog: dict[str, Any],
    aliases: dict[str, str],
    paths: list[str],
) -> int:
    """Rewrite entity objects found at the configured result paths. Returns objects touched."""
    if node is None:
        return 0
    for path in paths:
        objects = _collect(node, path)
        matched = [o for o in objects if isinstance(o, dict) and _looks_like_entity(o, entity, aliases)]
        if matched:
            for o in matched:
                _rewrite_object(o, entity, decision, program, catalog, aliases)
            return len(matched)
    return 0


def _collect(node: Any, path: str) -> list[Any]:
    if path == "":
        return [node]
    cur = [node]
    for part in path.split("."):
        nxt: list[Any] = []
        for c in cur:
            if part == "*":
                if isinstance(c, list):
                    nxt.extend(c)
                elif isinstance(c, dict):
                    nxt.extend(c.values())
            elif isinstance(c, dict) and part in c:
                nxt.append(c[part])
        cur = nxt
    return cur


def _looks_like_entity(obj: dict[str, Any], entity: EntityMapping, aliases: dict[str, str]) -> bool:
    names = {aliases.get(k, k) for k in obj}
    heads = {to_snake(f.split(".")[0]) for f in entity.fields}
    return any(to_snake(n) in heads for n in names)


def _rewrite_object(
    obj: dict[str, Any],
    entity: EntityMapping,
    decision: Decision,
    program: str,
    catalog: dict[str, Any],
    aliases: dict[str, str],
) -> None:
    for key in list(obj):
        field_name = aliases.get(key, key)
        attr = entity.attribute_for(field_name)
        sub = entity.sub_fields(field_name)
        if attr is not None:
            mode = decision.mode(attr)
            obj[key] = (
                None if mode == "deny" else transform(attr, obj[key], mode, program=program, catalog=catalog)
            )
        elif sub:
            value = obj[key]
            parsed = value
            was_str = isinstance(value, str)
            if was_str:
                try:
                    parsed = json.loads(value)
                except ValueError:
                    obj[key] = None
                    continue
            if isinstance(parsed, dict):
                out = {}
                for k2, v2 in parsed.items():
                    a2 = sub.get(k2) or sub.get(to_snake(k2))
                    if a2 is None:
                        out[k2] = v2
                    elif decision.mode(a2) != "deny":
                        out[k2] = transform(a2, v2, decision.mode(a2), program=program, catalog=catalog)
                obj[key] = json.dumps(out) if was_str else out
        elif isinstance(obj[key], dict):
            nested = _entity_for_nested(entity, field_name)
            if nested:
                _rewrite_object(obj[key], nested, decision, program, catalog, aliases)


def _entity_for_nested(entity: EntityMapping, field_name: str) -> EntityMapping | None:
    return None  # nested entities are rewritten when their own operation is queried


# ------------------------------------------------------------------------------------------
# REST (FHIR-style)
# ------------------------------------------------------------------------------------------


def _handle_rest(
    cfg: GatewayConfig, upstream: httpx.Client, request: Request, path: str, entity: EntityMapping
) -> Response:
    correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    purpose = request.headers.get("x-purpose")
    if not purpose:
        return _json_error(422, "purpose_required", "X-Purpose header is required")
    program = (
        cfg.mapping.program_for_code(request.headers.get("x-program"))
        if request.headers.get("x-program") not in cfg.mapping.programs_by_code.values()
        else request.headers["x-program"]
    )
    decision = _decide(cfg, request, entity, purpose, program, "read", correlation_id)
    if isinstance(decision, Response):
        return decision
    if not decision.allow:
        return _json_error(403, "policy_denied", "denied by policy", reason_codes=decision.reason_codes)
    r = upstream.get(
        path,
        params=dict(request.query_params),
        headers={k: v for k, v in request.headers.items() if k.lower() not in HOP},
    )
    if r.status_code != 200:
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")
    try:
        data = r.json()
    except ValueError:
        return Response(content=r.content, status_code=r.status_code)
    catalog = cfg.control_plane.catalog()
    resources = (
        [e.get("resource") for e in data.get("entry", [])] if data.get("resourceType") == "Bundle" else [data]
    )
    count = 0
    for res in resources:
        if isinstance(res, dict):
            _rewrite_object(res, entity, decision, program, catalog, {})
            count += 1
    _audit(cfg, request, entity, decision, purpose, program, path, count, correlation_id)
    return Response(
        content=json.dumps(data),
        status_code=200,
        media_type="application/json",
        headers={
            "X-Correlation-ID": correlation_id,
            "X-Policy-Version": decision.policy_version,
            "X-Obligations": ",".join(decision.obligations),
        },
    )


def get_path_value(obj: Any, path: str) -> Any:  # re-exported for configuration helpers
    return get_path(obj, path)
