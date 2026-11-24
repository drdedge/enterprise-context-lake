"""Application-side encryption of free-text columns (envelope encryption with AWS KMS).

The SQL Server database the lake runs on is shared and broadly readable, so the lake encrypts the
text of its records before they reach the database (ADR-010). Columns declared as ``SealedText`` in
the table definitions are encrypted when, and only when, the engine's dialect carries a cipher:

* SQLite and the Markdown vault never attach one, so those stores behave exactly as before.
* The SQL Server store attaches a ``KmsEnvelopeCipher`` (or a ``LocalKeyCipher`` in tests).

Envelope encryption: KMS issues a 256-bit data key (GenerateDataKey). The plaintext key stays in
process memory and encrypts values locally with AES-256-GCM; only the KMS-wrapped copy of the key is
stored, inside each value. Reading a value asks KMS to unwrap that key once and then caches it, so
KMS is called a handful of times per process, not per row. The data key is rotated after a set
number of values or hours. Each value is bound to its format version by the GCM associated data, so
tampering with any part of it fails decryption instead of returning altered text.

Stored format (ASCII, fits NVARCHAR(MAX)):  ``ecl1:<key_ref>:<wrapped_key_b64>:<nonce_b64>:<ct_b64>``

``key_ref`` is a short hash of the wrapped key, used only as the cache key. Values written before
encryption was switched on (plain text) are read back unchanged, so a store can be encrypted in place.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

PREFIX = "ecl1:"
_AAD = b"enterprise-context-lake/ecl1"
#: Encryption context sent to KMS with every call. KMS refuses to unwrap a key under another context,
#: and CloudTrail records it, so the key's use is attributable to the lake.
KMS_ENCRYPTION_CONTEXT = {"app": "enterprise-context-lake", "purpose": "column-encryption"}


class SealError(RuntimeError):
    """A sealed value could not be encrypted or decrypted (no cipher, wrong key, tampered value)."""


class FieldCipher(Protocol):
    def seal(self, plaintext: str) -> str: ...

    def open(self, sealed: str) -> str: ...


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


class _EnvelopeCipher:
    """AES-256-GCM with a rotating data key; subclasses say how data keys are made and unwrapped."""

    def __init__(self, *, rotate_after_values: int = 100_000, rotate_after_seconds: float = 24 * 3600) -> None:
        self._lock = threading.Lock()
        self._rotate_after_values = rotate_after_values
        self._rotate_after_seconds = rotate_after_seconds
        self._current: tuple[str, str, AESGCM] | None = None  # (key_ref, wrapped_b64, aead)
        self._current_uses = 0
        self._current_born = 0.0
        self._unwrapped: dict[str, AESGCM] = {}

    # Subclass hooks --------------------------------------------------------
    def _new_data_key(self) -> tuple[bytes, bytes]:  # (plaintext key, wrapped key)
        raise NotImplementedError

    def _unwrap(self, wrapped: bytes) -> bytes:
        raise NotImplementedError

    # -----------------------------------------------------------------------
    def _active_key(self) -> tuple[str, str, AESGCM]:
        with self._lock:
            now = time.monotonic()
            if (
                self._current is None
                or self._current_uses >= self._rotate_after_values
                or now - self._current_born >= self._rotate_after_seconds
            ):
                plain, wrapped = self._new_data_key()
                ref = hashlib.sha256(wrapped).hexdigest()[:16]
                aead = AESGCM(plain)
                self._current = (ref, _b64e(wrapped), aead)
                self._unwrapped[ref] = aead
                self._current_uses = 0
                self._current_born = now
            self._current_uses += 1
            return self._current

    def seal(self, plaintext: str) -> str:
        ref, wrapped_b64, aead = self._active_key()
        nonce = os.urandom(12)
        ct = aead.encrypt(nonce, plaintext.encode("utf-8"), _AAD)
        return f"{PREFIX}{ref}:{wrapped_b64}:{_b64e(nonce)}:{_b64e(ct)}"

    def open(self, sealed: str) -> str:
        try:
            ref, wrapped_b64, nonce_b64, ct_b64 = sealed[len(PREFIX) :].split(":")
        except ValueError as exc:
            raise SealError("Malformed sealed value") from exc
        aead = self._unwrapped.get(ref)
        if aead is None:
            wrapped = _b64d(wrapped_b64)
            if hashlib.sha256(wrapped).hexdigest()[:16] != ref:
                raise SealError("Sealed value key reference does not match its wrapped key")
            try:
                plain = self._unwrap(wrapped)
            except Exception as exc:
                raise SealError(f"Could not unwrap the data key: {type(exc).__name__}") from exc
            aead = AESGCM(plain)
            with self._lock:
                self._unwrapped[ref] = aead
        try:
            return aead.decrypt(_b64d(nonce_b64), _b64d(ct_b64), _AAD).decode("utf-8")
        except Exception as exc:
            raise SealError("Sealed value failed authentication (wrong key or tampered)") from exc


class KmsEnvelopeCipher(_EnvelopeCipher):
    """Data keys from AWS KMS (GenerateDataKey / Decrypt) under one customer-managed key.

    The IAM role of the app server needs ``kms:GenerateDataKey`` and ``kms:Decrypt`` on the key, with
    the encryption context above. ``endpoint_url`` points at a local KMS stand-in for tests.
    """

    def __init__(
        self,
        key_id: str,
        *,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        if not key_id:
            raise ValueError("A KMS key id or alias is required for column encryption")
        self.key_id = key_id
        if client is None:
            import boto3  # imported lazily: only the SQL Server deployment needs it

            client = boto3.client("kms", region_name=region_name, endpoint_url=endpoint_url)
        self._kms = client

    def _new_data_key(self) -> tuple[bytes, bytes]:
        resp = self._kms.generate_data_key(
            KeyId=self.key_id, KeySpec="AES_256", EncryptionContext=KMS_ENCRYPTION_CONTEXT
        )
        return resp["Plaintext"], resp["CiphertextBlob"]

    def _unwrap(self, wrapped: bytes) -> bytes:
        resp = self._kms.decrypt(
            CiphertextBlob=wrapped, KeyId=self.key_id, EncryptionContext=KMS_ENCRYPTION_CONTEXT
        )
        return resp["Plaintext"]

    def check(self) -> None:
        """Round-trip a value, so a missing permission fails at startup rather than on first write."""
        if self.open(self.seal("ok")) != "ok":  # pragma: no cover - would be a library bug
            raise SealError("KMS round trip returned the wrong value")


class LocalKeyCipher(_EnvelopeCipher):
    """Data keys wrapped by a local master key. For tests and local development only."""

    def __init__(self, master_key: bytes, **kw: Any) -> None:
        super().__init__(**kw)
        if len(master_key) != 32:
            raise ValueError("Local master key must be 32 bytes")
        self._master = AESGCM(master_key)

    def _new_data_key(self) -> tuple[bytes, bytes]:
        plain = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        return plain, nonce + self._master.encrypt(nonce, plain, b"local-wrap")

    def _unwrap(self, wrapped: bytes) -> bytes:
        return self._master.decrypt(wrapped[:12], wrapped[12:], b"local-wrap")


def attach_cipher(dialect: Any, cipher: FieldCipher | None) -> None:
    """Make ``SealedText`` columns encrypt on this engine's dialect (None switches it off)."""
    dialect._lake_cipher = cipher  # noqa: SLF001 - deliberate, read by SealedText below


class SealedText(TypeDecorator[str]):
    """Text column encrypted by the app when the engine carries a cipher; plain text otherwise."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        cipher = getattr(dialect, "_lake_cipher", None)
        if cipher is None or value is None:
            return value
        return cipher.seal(str(value))

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None or not isinstance(value, str) or not value.startswith(PREFIX):
            return value
        cipher = getattr(dialect, "_lake_cipher", None)
        if cipher is None:
            raise SealError("Encrypted value read without a cipher configured")
        return cipher.open(value)


__all__ = [
    "FieldCipher",
    "KmsEnvelopeCipher",
    "LocalKeyCipher",
    "SealError",
    "SealedText",
    "attach_cipher",
    "KMS_ENCRYPTION_CONTEXT",
]
