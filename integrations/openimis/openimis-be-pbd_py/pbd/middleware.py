"""Graphene middleware that turns every mapped GraphQL resolution into a policy-bound release.

How it works
------------
* At the top level (``Query``/``Mutation`` fields) the middleware records the operation, derives
  purpose, program and action (from ``X-Purpose``/``X-Program`` headers or the mapping's
  operation defaults) and, for vaulting-enabled mutations, rewrites the input so identifiers go
  to the Identity Vault instead of the openIMIS database.
* For fields of mapped entity types it asks the control plane for one decision per
  (entity, subject relationship) per request, caches it on the request, and transforms the
  resolved value to the release mode the decision prescribes (``deny`` becomes ``None``).
* JSON containers (``jsonExt``) are transformed key by key using the mapping's sub-fields.
* One ``read_access`` audit event per entity per request lists the subjects touched.
* If the control plane is unreachable and ``fail_closed`` is on, the field errors instead of
  leaking data.

Add to settings::

    GRAPHENE = {"MIDDLEWARE": ["pbd.middleware.PrivacyMiddleware", ...]}
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable
from typing import Any

from graphql import GraphQLError

from pbd_spmis.sdk import Decision, PolicyDeniedError, UnavailableError, transform
from pbd_spmis.sdk.mapping import EntityMapping, get_path, to_snake

from . import vaulting
from .actor import actor_for
from .config import PbdConfig, current

STATE_ATTR = "_pbd_state"


class RequestState:
    """Per-request scratch space stored on the GraphQL context (the Django request)."""

    def __init__(self) -> None:
        self.correlation_id = uuid.uuid4().hex
        self.operation: str | None = None
        self.purpose: str | None = None
        self.program: str | None = None
        self.action: str = "read"
        self.decisions: dict[tuple[str, tuple[str, ...]], Decision] = {}
        self.touched: dict[str, set[str]] = {}
        self.audited: set[str] = set()
        self.denied_entities: set[str] = set()


def _state(context: Any) -> RequestState:
    st = getattr(context, STATE_ATTR, None)
    if st is None:
        st = RequestState()
        with contextlib.suppress(Exception):  # immutable context objects
            setattr(context, STATE_ATTR, st)
    return st


def _header(context: Any, name: str) -> str | None:
    meta = getattr(context, "META", None) or {}
    key = "HTTP_" + name.upper().replace("-", "_")
    if key in meta:
        return meta[key]
    headers = getattr(context, "headers", None)
    if headers is not None:
        try:
            return headers.get(name)
        except Exception:  # noqa: BLE001
            return None
    return None


class PrivacyMiddleware:
    """Graphene 3 middleware (``resolve(next, root, info, **args)``)."""

    def __init__(self, config: PbdConfig | None = None):
        self._config = config

    @property
    def cfg(self) -> PbdConfig:
        return self._config or current()

    # ------------------------------------------------------------------ entry point
    def resolve(self, next_: Callable[..., Any], root: Any, info: Any, **args: Any) -> Any:
        cfg = self.cfg
        parent = info.parent_type.name
        context = info.context
        st = _state(context)

        if parent in ("Query", "Mutation") and root is None:
            self._enter_operation(st, info, args, cfg)
            if parent == "Mutation" and cfg.vault_identifiers:
                op = cfg.mapping.operation(info.field_name) or {}
                if op.get("vault"):
                    vaulting.rewrite_mutation_args(args, info.field_name, st, cfg)
            return next_(root, info, **args)

        entity = cfg.mapping.entity_for_type(parent)
        if entity is None:
            return next_(root, info, **args)

        field_name = info.field_name
        attr = entity.attribute_for(field_name)
        sub = entity.sub_fields(field_name)
        if attr is None and not sub:
            return next_(root, info, **args)

        decision = self._decision_for(st, entity, root, context, cfg)
        value = next_(root, info, **args)
        if decision is None or not decision.allow:
            return None
        program = st.program or cfg.mapping.default_program
        catalog = cfg.control_plane.catalog()
        if attr is not None:
            return transform(attr, value, decision.mode(attr), program=program, catalog=catalog)
        return _transform_container(value, sub, decision, program, catalog)

    # ------------------------------------------------------------------ helpers
    def _enter_operation(self, st: RequestState, info: Any, args: dict[str, Any], cfg: PbdConfig) -> None:
        name = info.field_name
        op = cfg.mapping.operation(name) or {}
        st.operation = name
        header_purpose = _header(info.context, "X-Purpose")
        st.purpose = header_purpose or (None if cfg.require_purpose_header else op.get("purpose"))
        st.action = str(op.get("action") or ("write" if info.parent_type.name == "Mutation" else "read"))
        code = _header(info.context, "X-Program") or _benefit_plan_code(args)
        st.program = (
            code if code in cfg.mapping.programs_by_code.values() else cfg.mapping.program_for_code(code)
        )
        cid = _header(info.context, "X-Correlation-ID")
        if cid:
            st.correlation_id = cid

    def _decision_for(
        self, st: RequestState, entity: EntityMapping, obj: Any, context: Any, cfg: PbdConfig
    ) -> Decision | None:
        subject = entity.subject_token(obj)
        programs = self._relationships(obj, st, cfg)
        key = (entity.name, tuple(sorted(programs)))
        decision = st.decisions.get(key)
        if decision is None:
            decision = self._decide(st, entity, subject, programs, context, cfg)
            st.decisions[key] = decision
        if subject:
            st.touched.setdefault(entity.name, set()).add(subject)
        self._audit_once(st, entity, decision, cfg)
        # A permitted request for which this entity releases nothing simply yields nulls; a failed
        # gate (role, purpose, assignment, grant...) is reported as an error with its reason codes.
        if not decision.allow and decision.reason_codes != ["NOTHING_RELEASABLE"]:
            raise GraphQLError(
                f"privacy: denied ({', '.join(decision.reason_codes)})",
                extensions={"decision_id": decision.decision_id, "reason_codes": decision.reason_codes},
            )
        return decision

    def _decide(
        self,
        st: RequestState,
        entity: EntityMapping,
        subject: str | None,
        programs: list[str],
        context: Any,
        cfg: PbdConfig,
    ) -> Decision:
        program = st.program or cfg.mapping.default_program
        actor = actor_for(getattr(context, "user", None), cfg.mapping, agency=cfg.agency, programs=[program])
        if actor is None:
            raise GraphQLError("privacy: no PbD role could be derived for this user")
        if not st.purpose:
            raise GraphQLError("privacy: X-Purpose header is required for this operation")
        try:
            return cfg.control_plane.pdp.decide(
                actor=actor,
                program=program,
                purpose=st.purpose,
                action=st.action,
                attributes=list(entity.attributes),
                subject_token=subject,
                subject_programs=programs,
                context={
                    "channel": _header(context, "X-Channel") or "openimis",
                    "device_trust": _header(context, "X-Device-Trust") or "managed",
                    "case_id": _header(context, "X-Case-ID"),
                    "correlation_id": st.correlation_id,
                },
                correlation_id=st.correlation_id,
            )
        except PolicyDeniedError as exc:
            raise GraphQLError(f"privacy: denied ({', '.join(exc.reason_codes)})") from exc
        except UnavailableError as exc:
            if cfg.fail_closed:
                raise GraphQLError("privacy: control plane unavailable; failing closed") from exc
            return Decision(allow=False, decision_id="", policy_version="", reason_codes=["UNAVAILABLE"])

    def _relationships(self, obj: Any, st: RequestState, cfg: PbdConfig) -> list[str]:
        if cfg.relationship_mode == "resolver" and cfg.relationship_resolver:
            fn = _import(cfg.relationship_resolver)
            return [str(p) for p in (fn(obj) or [])]
        explicit = get_path(obj, "pbd_programs")
        if explicit:
            return [str(p) for p in explicit]
        return [st.program or cfg.mapping.default_program]

    def _audit_once(
        self, st: RequestState, entity: EntityMapping, decision: Decision, cfg: PbdConfig
    ) -> None:
        if not cfg.audit_reads or entity.name in st.audited or not decision.decision_id:
            return
        st.audited.add(entity.name)
        try:
            cfg.control_plane.audit.emit(
                "read_access",
                service="openimis",
                actor_id="openimis:" + st.operation if st.operation else "openimis",
                outcome="allow" if decision.allow else "deny",
                purpose=st.purpose,
                program=st.program,
                attributes=decision.released(),
                decision_id=decision.decision_id,
                obligations=decision.obligations,
                correlation_id=st.correlation_id,
                details={"entity": entity.name, "operation": st.operation, "release": decision.release},
            )
        except UnavailableError:
            if cfg.fail_closed:
                raise GraphQLError("privacy: audit store unavailable; failing closed") from None


def _transform_container(
    value: Any, sub: dict[str, str], decision: Decision, program: str, catalog: dict[str, Any]
) -> Any:
    """Apply per-key release modes inside a JSON container such as ``jsonExt``."""
    if value is None:
        return None
    parsed: Any = value
    was_str = isinstance(value, str)
    if was_str:
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
    if not isinstance(parsed, dict):
        return None
    out: dict[str, Any] = {}
    for key, v in parsed.items():
        attr = sub.get(key) or sub.get(to_snake(key))
        if attr is None:
            out[key] = v  # not personal data according to the mapping
            continue
        mode = decision.mode(attr)
        if mode == "deny":
            continue
        out[key] = transform(attr, v, mode, program=program, catalog=catalog)
    return json.dumps(out) if was_str else out


def _benefit_plan_code(args: dict[str, Any]) -> str | None:
    for key in ("benefitPlanCode", "benefit_plan_code", "benefitPlan_Code"):
        if key in args:
            return str(args[key])
    bp = args.get("benefitPlan") or args.get("benefit_plan")
    if isinstance(bp, dict):
        return bp.get("code")
    return None


def _import(path: str) -> Callable[..., Any]:
    module, _, name = path.rpartition(".")
    mod = __import__(module, fromlist=[name])
    return getattr(mod, name)
