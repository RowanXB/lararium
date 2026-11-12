"""Credential encryption: AES-256-GCM under the master key (LARARIUM_MASTER_KEY).

This is the only module that touches the master key's cipher. Plaintext leaves it wrapped
in a Secret, which prints as "Secret(***)"; call reveal() only where the value is sent.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from lararium.core.errors import CredentialDecryptError

KEY_BYTES = 32
NONCE_BYTES = 12
# Stored with each credential, so the master key can be rotated later.
KEY_VERSION = 1


class Secret:
    """A decrypted credential that never shows its value in repr, str or f-strings."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


def load_master_key(value: SecretStr) -> bytes:
    """Decode the base64 master key; it must be exactly 32 bytes."""
    # The messages never include the value.
    try:
        key = base64.b64decode(value.get_secret_value(), validate=True)
    except binascii.Error:
        raise ValueError("the master key must be base64 of 32 bytes") from None
    if len(key) != KEY_BYTES:
        raise ValueError(f"the master key must be base64 of 32 bytes, got {len(key)} bytes")
    return key


def encrypt(master_key: bytes, plaintext: str, aad: str) -> tuple[bytes, bytes]:
    """Encrypt under a fresh random nonce; return (nonce, ciphertext)."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(master_key).encrypt(nonce, plaintext.encode(), aad.encode())


def decrypt(master_key: bytes, nonce: bytes, ciphertext: bytes, aad: str) -> Secret:
    """Decrypt and authenticate; any failure raises CredentialDecryptError."""
    try:
        plaintext = AESGCM(master_key).decrypt(nonce, ciphertext, aad.encode())
        return Secret(plaintext.decode())
    except (InvalidTag, ValueError):
        raise CredentialDecryptError(f"can't decrypt the credential {aad}") from None
