"""Vault identifiers that already live in openIMIS records (one-off migration).

``vault_record`` is the pure core: given a record's ``json_ext``, the fields to vault and a vault
client, it returns the rewritten ``json_ext`` (placeholder plus person token) or ``None`` when
nothing needs to change. The management command ``pbd_vault_identifiers`` applies it to
``Individual`` rows in batches, with ``--dry-run``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pbd_spmis.sdk import PrivacyControlPlane
from pbd_spmis.sdk.mapping import get_path, set_path

DEFAULT_FIELDS = {"national_id": "national_id", "contact": "email"}


def vault_record(
    json_ext: dict[str, Any] | None,
    *,
    name: str,
    program: str,
    vault: PrivacyControlPlane | Any,
    fields: dict[str, str] | None = None,
    placeholder: str = "vaulted",
    token_key: str = "pbd_person_token",  # noqa: S107 - a JSON key name, not a secret
    proofing_reference: str = "openimis:migration",
    tokenize: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return the rewritten ``json_ext`` or ``None`` if the record holds no identifier to vault."""
    fields = fields or DEFAULT_FIELDS
    ext = dict(json_ext or {})
    national_id = get_path(ext, fields["national_id"]) if fields.get("national_id") else None
    if not national_id or national_id == placeholder:
        return None
    contact_path = fields.get("contact")
    contact = get_path(ext, contact_path) if contact_path else None
    call = tokenize or vault.vault.tokenize
    result = call(
        national_id=str(national_id),
        name=name,
        contact=str(contact) if contact and contact != placeholder else "",
        program=program,
        proofing_reference=proofing_reference,
    )
    set_path(ext, fields["national_id"], placeholder)
    if contact_path and contact:
        set_path(ext, contact_path, placeholder)
    ext[token_key] = result["person_token"]
    return ext


def migrate_individuals(
    individuals: Any,
    *,
    program: str,
    vault: PrivacyControlPlane,
    fields: dict[str, str] | None = None,
    placeholder: str = "vaulted",
    token_key: str = "pbd_person_token",  # noqa: S107 - a JSON key name, not a secret
    dry_run: bool = False,
    save: Callable[[Any, dict[str, Any]], None] | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Apply :func:`vault_record` to an iterable of openIMIS Individual-like objects."""
    summary = {"scanned": 0, "vaulted": 0, "skipped": 0, "failed": 0}
    for ind in individuals:
        summary["scanned"] += 1
        name = " ".join(p for p in (getattr(ind, "first_name", ""), getattr(ind, "last_name", "")) if p)
        try:
            new_ext = vault_record(
                getattr(ind, "json_ext", None),
                name=name,
                program=program,
                vault=vault,
                fields=fields,
                placeholder=placeholder,
                token_key=token_key,
            )
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            summary["failed"] += 1
            log(f"FAILED {getattr(ind, 'id', '?')}: {exc.__class__.__name__}: {exc}")
            continue
        if new_ext is None:
            summary["skipped"] += 1
            continue
        if dry_run:
            log(f"would vault individual {getattr(ind, 'id', '?')}")
        else:
            if save is not None:
                save(ind, new_ext)
            else:
                ind.json_ext = new_ext
                ind.save()
        summary["vaulted"] += 1
    return summary
