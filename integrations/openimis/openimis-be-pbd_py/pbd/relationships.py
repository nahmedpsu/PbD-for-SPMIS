"""Subject relationship resolvers.

The control plane's ``requires_relationship`` gate asks which programs a subject is related to.
openIMIS records that relationship as ``Beneficiary`` (individual x benefit plan) and
``GroupBeneficiary`` (group x benefit plan) rows. ``from_beneficiaries`` reads them, so a case
worker assigned to one program cannot read a person who is only enrolled in another, which is
exactly the cross-program isolation test of the architecture guide.

Enable with::

    relationship_mode: resolver
    relationship_resolver: pbd.relationships.from_beneficiaries

The resolver is pure over the objects it is given; ``_benefit_plan_codes`` isolates the ORM
access so the logic can be unit-tested with plain objects.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any

from .config import current


def from_beneficiaries(obj: Any) -> list[str]:
    """Programs (catalogue names) the resolved object's subject is related to."""
    codes = _benefit_plan_codes(obj)
    mapping = current().mapping
    programs = {mapping.program_for_code(code) for code in codes if code}
    return sorted(p for p in programs if p)


def programs_for_codes(codes: Iterable[str]) -> list[str]:
    mapping = current().mapping
    return sorted({mapping.program_for_code(c) for c in codes if c})


def _benefit_plan_codes(obj: Any) -> set[str]:
    """Benefit plan codes linked to an Individual, Beneficiary, Group or GroupBeneficiary."""
    explicit = getattr(obj, "pbd_benefit_plan_codes", None)
    if explicit is not None:
        return {str(c) for c in explicit}

    kind = type(obj).__name__
    codes: set[str] = set()
    if kind == "Beneficiary":
        plan = getattr(obj, "benefit_plan", None)
        if plan is not None and getattr(plan, "code", None):
            codes.add(str(plan.code))
        individual = getattr(obj, "individual", None)
        if individual is not None:
            codes |= _codes_for_individual(individual)
        return codes
    if kind == "GroupBeneficiary":
        plan = getattr(obj, "benefit_plan", None)
        if plan is not None and getattr(plan, "code", None):
            codes.add(str(plan.code))
        group = getattr(obj, "group", None)
        if group is not None:
            codes |= _codes_for_group(group)
        return codes
    if kind == "Group":
        return _codes_for_group(obj)
    if kind == "Individual":
        return _codes_for_individual(obj)
    # Unknown object: try both shapes before giving up.
    return _codes_for_individual(obj) | _codes_for_group(obj)


def _codes_for_individual(individual: Any) -> set[str]:
    codes: set[str] = set()
    try:
        from social_protection.models import Beneficiary, GroupBeneficiary  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - openIMIS not present (unit tests)
        return codes
    pk = getattr(individual, "id", None) or getattr(individual, "pk", None)
    if pk is None:
        return codes
    codes |= {
        str(c)
        for c in Beneficiary.objects.filter(individual_id=pk, is_deleted=False).values_list(
            "benefit_plan__code", flat=True
        )
    }
    with contextlib.suppress(Exception):  # group modules optional
        from individual.models import GroupIndividual  # type: ignore[import-not-found]

        group_ids = list(
            GroupIndividual.objects.filter(individual_id=pk, is_deleted=False).values_list(
                "group_id", flat=True
            )
        )
        if group_ids:
            codes |= {
                str(c)
                for c in GroupBeneficiary.objects.filter(
                    group_id__in=group_ids, is_deleted=False
                ).values_list("benefit_plan__code", flat=True)
            }
    return codes


def _codes_for_group(group: Any) -> set[str]:
    try:
        from social_protection.models import GroupBeneficiary  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return set()
    pk = getattr(group, "id", None) or getattr(group, "pk", None)
    if pk is None:
        return set()
    return {
        str(c)
        for c in GroupBeneficiary.objects.filter(group_id=pk, is_deleted=False).values_list(
            "benefit_plan__code", flat=True
        )
    }
