import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from MpesaSDK.crypto import encrypt_token, load_public_key
from MpesaSDK.errors import CryptoError


def test_round_trip_with_base64_der_key(private_key, public_key_b64):
    token = encrypt_token("my-api-key", load_public_key(public_key_b64))
    plain = private_key.decrypt(base64.b64decode(token), padding.PKCS1v15())
    assert plain == b"my-api-key"


def test_accepts_pem_and_wrapped_base64(private_key, public_key_b64):
    pem = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    wrapped = "\n".join(public_key_b64[i : i + 64] for i in range(0, len(public_key_b64), 64))
    for form in (pem, wrapped):
        encrypt_token("x", load_public_key(form))


def test_padding_is_randomised(public_key_b64):
    key = load_public_key(public_key_b64)
    assert encrypt_token("same", key) != encrypt_token("same", key)


@pytest.mark.parametrize(
    "bad", ["not base64 !!", "AAAA", "-----BEGIN PUBLIC KEY-----\nzz\n-----END PUBLIC KEY-----"]
)
def test_bad_keys_raise_crypto_error(bad):
    with pytest.raises(CryptoError):
        load_public_key(bad)


def test_token_too_long_for_key(public_key_b64):
    with pytest.raises(CryptoError):
        encrypt_token("x" * 1000, load_public_key(public_key_b64))
