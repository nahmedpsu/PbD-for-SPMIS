"""Identifier vaulting for create/update mutations (opt-in).

When enabled, the national identifier (and optionally contact) supplied to ``createIndividual``
and ``updateIndividual`` is sent to the Identity Vault under the ``identity_proofing`` purpose,
and the mutation input is rewritten so that openIMIS stores a placeholder plus the person token.
The openIMIS database then never holds the identifier; the module resolves it on demand through
the vault's policy-gated endpoint when a purpose permits.
"""

from __future__ import annotations

from typing import Any

from graphql import GraphQLError

from pbd_spmis.sdk import PrivacyError, UnavailableError
from pbd_spmis.sdk.mapping import get_path, set_path

from .config import PbdConfig


def _read(args: dict[str, Any], spec: Any) -> str | None:
    if isinstance(spec, list):
        parts = [str(get_path(args, p) or "").strip() for p in spec]
        joined = " ".join(p for p in parts if p)
        return joined or None
    v = get_path(args, str(spec))
    return str(v) if v not in (None, "") else None


def rewrite_mutation_args(args: dict[str, Any], operation: str, st: Any, cfg: PbdConfig) -> None:
    spec = (cfg.mapping.vault.get("input_fields") or {}).get(operation)
    if not spec:
        return
    _materialise(args)
    national_id = _read(args, spec.get("national_id"))
    if not national_id or national_id == cfg.mapping.vault.get("placeholder", "vaulted"):
        return
    name = _read(args, spec.get("name")) or ""
    contact = _read(args, spec.get("contact")) or ""
    program = st.program or cfg.mapping.default_program
    try:
        result = cfg.control_plane.vault.tokenize(
            national_id=national_id,
            name=name,
            contact=contact,
            program=program,
            proofing_reference=f"openimis:{operation}",
            correlation_id=st.correlation_id,
        )
    except (UnavailableError, PrivacyError) as exc:
        raise GraphQLError(f"privacy: identity vault refused the identifier ({exc.code})") from exc
    token = result["person_token"]
    placeholder = cfg.mapping.vault.get("placeholder", "vaulted")
    set_path(args, str(spec["national_id"]), placeholder)
    if spec.get("contact"):
        set_path(args, str(spec["contact"]), placeholder)
    set_path(args, str(cfg.mapping.vault.get("token_field", "input.jsonExt.pbd_person_token")), token)


def _materialise(args: dict[str, Any]) -> None:
    """graphene passes InputObjectType instances (dict subclasses); make nested values plain dicts."""
    for k, v in list(args.items()):
        if isinstance(v, dict) and type(v) is not dict:  # noqa: E721 - InputObjectType subclass
            args[k] = dict(v)
            _materialise(args[k])
        elif isinstance(v, dict):
            _materialise(v)
