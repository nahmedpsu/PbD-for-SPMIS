"""Opaque identifier generation.

Identifiers never encode anything about the person. They are random, prefixed so that a
reviewer can tell a person token from a household token or a decision id at a glance, and
short enough to appear in URLs and audit logs safely.
"""

from __future__ import annotations

import secrets
import string
import time

_ALPHABET = string.ascii_uppercase + string.digits


def _rand(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def person_token() -> str:
    return f"P-{_rand(8)}"


def household_token() -> str:
    return f"H-{_rand(8)}"


def program_person_id(program_code: str) -> str:
    return f"{program_code.upper()}-{_rand(8)}"


def decision_id() -> str:
    return f"PD-{int(time.time() * 1000):x}-{_rand(6)}"


def payment_token() -> str:
    return f"PT-{_rand(10)}"


def correlation_id() -> str:
    return secrets.token_hex(16)


def opaque(prefix: str, n: int = 10) -> str:
    return f"{prefix}-{_rand(n)}"
