"""RSA encryption of the API key / session key, as required by the OpenAPI.

Both the API key (to obtain a session) and the session key (for every other call) are
sent as ``Authorization: Bearer <base64(RSA-PKCS1v1.5(key))>``.
"""

from __future__ import annotations

import base64
import binascii

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import (
    load_der_public_key,
    load_pem_public_key,
)

from .errors import CryptoError


def load_public_key(public_key: str) -> rsa.RSAPublicKey:
    """Load a PEM block or the bare base64 DER string shown in the developer portal."""
    text = public_key.strip()
    try:
        if text.startswith("-----BEGIN"):
            key = load_pem_public_key(text.encode())
        else:
            key = load_der_public_key(base64.b64decode("".join(text.split()), validate=True))
    except (ValueError, binascii.Error) as exc:
        raise CryptoError("public-key is not a valid RSA public key") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise CryptoError("public-key must be an RSA key")
    return key


def encrypt_token(token: str, public_key: rsa.RSAPublicKey) -> str:
    """Return ``base64(RSA(token))``. Padding is random, so output differs per call."""
    try:
        ciphertext = public_key.encrypt(token.encode(), padding.PKCS1v15())
    except ValueError as exc:  # token too long for the key size
        raise CryptoError("Could not encrypt token with the supplied public key") from exc
    return base64.b64encode(ciphertext).decode("ascii")
