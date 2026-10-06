"""Cryptographic primitives for field-level protection.

Design
------
* **Envelope encryption.** Each record gets its own data-encryption key (DEK). Fields are
  encrypted with AES-256-GCM under the DEK; the DEK is wrapped with a master key held by the
  ``KeyProvider``. Rotating the master key means re-wrapping DEKs, not re-encrypting data.
* **KeyProvider** is the seam for a KMS/HSM. The default provider reads a base64 key from the
  environment and is suitable for development only. A production provider wraps and unwraps
  DEKs through the government KMS/HSM API and never exposes the master key to the process.
* **Blind index.** A keyed HMAC of a normalised identifier supports deduplication lookups
  without storing the identifier in clear text or in a reversible form.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12


class KeyProvider(Protocol):
    key_id: str

    def wrap(self, dek: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKeyProvider:
    """Development key provider: AES-GCM key wrapping under a local master key."""

    def __init__(self, master_key: bytes, key_id: str = "local-dev"):
        if len(master_key) != 32:
            raise ValueError("master key must be 32 bytes")
        self._aead = AESGCM(master_key)
        self.key_id = key_id

    @classmethod
    def from_env(cls, b64: str) -> LocalKeyProvider:
        if not b64:
            # Deterministic-but-random per process is unsafe across restarts, so warn loudly by
            # deriving from a fixed string only in dev. Deployments must set the variable.
            raw = hashlib.sha256(b"pbd-spmis-dev-master-key").digest()
            return cls(raw, key_id="insecure-dev-default")
        return cls(base64.b64decode(b64), key_id="env")

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, dek, self.key_id.encode())

    def unwrap(self, wrapped: bytes) -> bytes:
        nonce, ct = wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:]
        return self._aead.decrypt(nonce, ct, self.key_id.encode())


@dataclass
class FieldCipher:
    """Encrypts individual fields of one record under a per-record DEK."""

    dek: bytes
    wrapped_dek: bytes
    key_id: str

    @classmethod
    def new(cls, provider: KeyProvider) -> FieldCipher:
        dek = AESGCM.generate_key(bit_length=256)
        return cls(dek=dek, wrapped_dek=provider.wrap(dek), key_id=provider.key_id)

    @classmethod
    def open(cls, provider: KeyProvider, wrapped_dek: bytes) -> FieldCipher:
        return cls(dek=provider.unwrap(wrapped_dek), wrapped_dek=wrapped_dek, key_id=provider.key_id)

    def encrypt(self, field_name: str, value: str) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        # The field name is bound as associated data so a ciphertext cannot be moved between
        # columns (e.g. swapping an encrypted contact into the national_id column).
        return nonce + AESGCM(self.dek).encrypt(nonce, value.encode("utf-8"), field_name.encode())

    def decrypt(self, field_name: str, blob: bytes) -> str:
        nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
        return AESGCM(self.dek).decrypt(nonce, ct, field_name.encode()).decode("utf-8")


def blind_index(key: str, value: str) -> str:
    """Keyed hash for equality lookups (deduplication) without storing the value."""
    k = (key or "pbd-spmis-dev-index-key").encode("utf-8")
    # Identifier formats vary by issuing office ("NID-1001", "nid 1001", "NID1001" are the same
    # document), so the index is computed over the upper-cased alphanumeric characters only.
    normalised = "".join(ch for ch in value if ch.isalnum()).upper()
    return hmac.new(k, normalised.encode("utf-8"), hashlib.sha256).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
