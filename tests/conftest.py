"""Shared fixtures: an RSA key pair and a scriptable fake of the M-Pesa OpenAPI."""

from __future__ import annotations

import base64
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import MpesaSDK as mp

API_KEY = "test-api-key-0123456789"


@dataclass
class Reply:
    status: int
    body: dict[str, Any] = field(default_factory=dict)


def sync_ok(tx_id: str = "49XCD123F6") -> Reply:
    return Reply(
        201,
        {
            "output_ResponseCode": "INS-0",
            "output_ResponseDesc": "Request processed successfully",
            "output_TransactionID": tx_id,
            "output_ConversationID": "conv-1",
            "output_ThirdPartyConversationID": "ignored",
        },
    )


def async_accepted() -> Reply:
    return Reply(
        201,
        {
            "output_ResponseCode": "INS-0",
            "output_ResponseDesc": "Successfully Accepted Request",
            "output_ConversationID": "conv-1",
        },
    )


def error(code: str, status: int = 400, desc: str | None = None) -> Reply:
    return Reply(status, {"output_ResponseCode": code, "output_ResponseDesc": desc or code})


class FakeMpesa:
    """httpx handler. Queue replies/exceptions with ``script``; default is sync success."""

    def __init__(self, private_key: rsa.RSAPrivateKey) -> None:
        self._key = private_key
        self._script: deque[Reply | Exception] = deque()
        self.session_script: deque[Reply | Exception] = deque()
        self.sessions_issued = 0
        self.payment_requests: list[httpx.Request] = []
        self.session_requests: list[httpx.Request] = []
        self.tokens: list[str] = []

    def script(self, *items: Reply | Exception) -> FakeMpesa:
        self._script.extend(items)
        return self

    @property
    def payment_bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.payment_requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = self._decrypt(request.headers.get("Authorization", ""))
        if token is None:
            return httpx.Response(401, text="unauthorised")
        self.tokens.append(token)

        if request.url.path.endswith("/getSession/"):
            self.session_requests.append(request)
            item = self.session_script.popleft() if self.session_script else None
            if isinstance(item, Exception):
                raise item
            if item is not None:
                return httpx.Response(item.status, json=item.body)
            if token != API_KEY:
                return httpx.Response(400, json={"output_ResponseCode": "INS-989"})
            self.sessions_issued += 1
            return httpx.Response(
                200,
                json={
                    "output_ResponseCode": "INS-0",
                    "output_ResponseDesc": "Request processed successfully",
                    "output_SessionID": f"SESSION-{self.sessions_issued}",
                },
            )

        self.payment_requests.append(request)
        if not token.startswith("SESSION-"):
            return httpx.Response(401, text="bad session")
        item = self._script.popleft() if self._script else sync_ok()
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item.status, json=item.body)

    def _decrypt(self, header: str) -> str | None:
        if not header.startswith("Bearer "):
            return None
        try:
            cipher = base64.b64decode(header.removeprefix("Bearer "))
            return self._key.decrypt(cipher, padding.PKCS1v15()).decode()
        except ValueError:
            return None


@pytest.fixture(scope="session")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def public_key_b64(private_key) -> str:
    der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode()


@pytest.fixture
def fake(private_key) -> FakeMpesa:
    return FakeMpesa(private_key)


@pytest.fixture
def settings(public_key_b64) -> dict[str, Any]:
    """Fast settings: zero back-off so retry tests do not sleep."""
    return {
        "market": mp.markets.LESOTHO,
        "api-key": API_KEY,
        "public-key": public_key_b64,
        "live": False,
        "retry-strategy": {"retry-count": 3, "back-off": 0.0, "jitter": 0.0},
    }


@pytest.fixture
def make_provider(settings, fake):
    def build(**overrides: Any) -> mp.MpesaProvider:
        return mp.config({**settings, **overrides}, transport=httpx.MockTransport(fake))

    return build


@pytest.fixture
def provider(make_provider) -> mp.MpesaProvider:
    return make_provider()


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "amount": "10.50",
        "customer_msisdn": "26658123456",
        "service_provider_code": "ORG001",
        "reference": "T12344C",
        "description": "Handbag, Black",
        "third_party_conversation_id": "tp0001",
    }
