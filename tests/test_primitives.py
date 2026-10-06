from __future__ import annotations

import base64
import os
import time

import pytest

from pbd_spmis.audit.app import EventIn, pii_guard
from pbd_spmis.common.auth import issue_token, verify_token
from pbd_spmis.common.crypto import FieldCipher, LocalKeyProvider, blind_index
from pbd_spmis.common.errors import AuthenticationError, ValidationFailed


def test_envelope_encryption_round_trip_and_column_binding():
    provider = LocalKeyProvider(os.urandom(32), key_id="t")
    cipher = FieldCipher.new(provider)
    ct = cipher.encrypt("national_id", "NID-1001")
    assert cipher.decrypt("national_id", ct) == "NID-1001"
    reopened = FieldCipher.open(provider, cipher.wrapped_dek)
    assert reopened.decrypt("national_id", ct) == "NID-1001"
    with pytest.raises(Exception):  # noqa: B017 - AES-GCM tag failure
        reopened.decrypt("contact", ct)


def test_master_key_rotation_only_rewraps_dek():
    old = LocalKeyProvider(os.urandom(32), key_id="k1")
    new = LocalKeyProvider(os.urandom(32), key_id="k2")
    cipher = FieldCipher.new(old)
    ct = cipher.encrypt("name", "Amina")
    rewrapped = new.wrap(old.unwrap(cipher.wrapped_dek))
    assert FieldCipher.open(new, rewrapped).decrypt("name", ct) == "Amina"


def test_local_provider_from_env():
    key = base64.b64encode(os.urandom(32)).decode()
    assert LocalKeyProvider.from_env(key).key_id == "env"
    assert LocalKeyProvider.from_env("").key_id == "insecure-dev-default"


def test_blind_index_normalises_and_is_keyed():
    assert blind_index("k", "nid 1001") == blind_index("k", "NID1001") == blind_index("k", "NID-1001")
    assert blind_index("k", "NID1001") != blind_index("other", "NID1001")


def test_jwt_round_trip_and_tamper_detection():
    tok = issue_token(
        "secret", sub="u1", role="CASE_WORKER", agency="A", programs=["cash_assistance"], amr=["pwd", "mfa"]
    )
    actor = verify_token("secret", tok, issuer="pbd-spmis-dev")
    assert actor.id == "u1" and actor.has_mfa and actor.programs == ("cash_assistance",)
    with pytest.raises(AuthenticationError):
        verify_token("wrong", tok)
    h, p, s = tok.split(".")
    with pytest.raises(AuthenticationError):
        verify_token("secret", f"{h}.{p}x.{s}")
    expired = issue_token("secret", sub="u1", role="X", agency="A", ttl_seconds=-1)
    time.sleep(0.01)
    with pytest.raises(AuthenticationError):
        verify_token("secret", expired)


def test_audit_pii_guard():
    pii_guard({"release": {"national_id": "deny", "name": "masked"}, "rows": 3})
    with pytest.raises(ValidationFailed):
        pii_guard({"national_id": "NID-1001"})
    with pytest.raises(ValidationFailed):
        pii_guard({"nested": [{"contact": "+1"}]})
    with pytest.raises(ValidationFailed):
        pii_guard({"note": "person NID-1001 called"})
    with pytest.raises(ValidationFailed):
        pii_guard({"note": "x" * 300})
    EventIn(event_type="t", service="s", actor_id="a", outcome="ok")
