"""Thin async HTTP layer: builds URLs/headers and never raises on transport problems."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .crypto import encrypt_token, load_public_key
from .settings import MpesaConfig

logger = logging.getLogger("MpesaSDK")


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status_code: int | None
    body: dict[str, Any] = field(default_factory=dict)
    network_error: str | None = None

    @property
    def code(self) -> str | None:
        value = self.body.get("output_ResponseCode")
        return str(value) if value is not None else None

    @property
    def description(self) -> str | None:
        value = self.body.get("output_ResponseDesc")
        return str(value) if value is not None else None


class MpesaHttpClient:
    def __init__(self, config: MpesaConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._config = config
        self._transport = transport
        self._public_key = load_public_key(config.public_key)  # fail fast on a bad key

    def url(self, path: str) -> str:
        c = self._config
        return f"{c.base_url.rstrip('/')}/{c.environment}/ipg/v2/{c.market.context}/{path}"

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {encrypt_token(token, self._public_key)}",
            "Origin": self._config.origin,
        }

    async def get_session(self) -> ApiResponse:
        return await self._send("GET", "getSession/", self._config.api_key, None)

    async def post(self, path: str, session_key: str, payload: dict[str, Any]) -> ApiResponse:
        return await self._send("POST", path, session_key, payload)

    async def _send(
        self, method: str, path: str, token: str, payload: dict[str, Any] | None
    ) -> ApiResponse:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._config.request_timeout
            ) as client:
                response = await client.request(
                    method, self.url(path), headers=self._headers(token), json=payload
                )
        except httpx.HTTPError as exc:
            logger.warning("M-Pesa %s %s failed: %s", method, path, type(exc).__name__)
            return ApiResponse(None, network_error=f"{type(exc).__name__}: {exc}")
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            body = {"raw": response.text[:2000]} if response.text else {}
        return ApiResponse(response.status_code, body)
