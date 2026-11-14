import base64

import pytest
from pydantic import SecretStr

from lararium.config import load_master_key
from lararium.core.errors import CredentialDecryptError
from lararium.core.secrets import Secret, decrypt, encrypt

KEY = bytes(range(32))


def test_encrypt_round_trip_and_aad_binding() -> None:
    nonce, ciphertext = encrypt(KEY, "tok-123", "conn-1:access_token")
    assert len(nonce) == 12
    assert decrypt(KEY, nonce, ciphertext, "conn-1:access_token").reveal() == "tok-123"
    with pytest.raises(CredentialDecryptError):
        decrypt(KEY, nonce, ciphertext, "conn-2:access_token")
    with pytest.raises(CredentialDecryptError):
        decrypt(bytes(32), nonce, ciphertext, "conn-1:access_token")


def test_each_encryption_uses_a_fresh_nonce() -> None:
    first = encrypt(KEY, "tok-123", "conn-1:access_token")
    second = encrypt(KEY, "tok-123", "conn-1:access_token")
    assert first[0] != second[0]
    assert first[1] != second[1]


def test_secret_never_reprs_plaintext() -> None:
    secret = Secret("hunter2")
    for shown in (repr(secret), str(secret), f"{secret}", f"{secret!r}", repr([secret])):
        assert "hunter2" not in shown
    assert repr(secret) == str(secret) == "Secret(***)"
    assert secret.reveal() == "hunter2"


def test_master_key_must_be_32_bytes() -> None:
    assert load_master_key(SecretStr(base64.b64encode(KEY).decode())) == KEY
    for bad in (base64.b64encode(bytes(16)).decode(), "not base64!", ""):
        with pytest.raises(ValueError, match="32 bytes") as raised:
            load_master_key(SecretStr(bad))
        assert bad == "" or bad not in str(raised.value)
