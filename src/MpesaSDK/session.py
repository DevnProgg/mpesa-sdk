"""Session-key cache: one ``getSession`` call is shared by all transactions in a process."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from .errors import SessionError
from .http import MpesaHttpClient
from .settings import MpesaConfig


class SessionManager:
    def __init__(
        self,
        config: MpesaConfig,
        client: MpesaHttpClient,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._session: str | None = None
        self._expires_at = 0.0
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _loop_lock(self) -> asyncio.Lock:
        # Celery runs each task in a fresh event loop; a lock must belong to the running one.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def _valid(self) -> bool:
        return self._session is not None and self._clock() < self._expires_at

    async def get(self) -> str:
        if self._valid():
            return self._session  # type: ignore[return-value]
        async with self._loop_lock():
            if self._valid():
                return self._session  # type: ignore[return-value]
            response = await self._client.get_session()
            session_id = response.body.get("output_SessionID")
            if response.code != "INS-0" or not session_id:
                reason = (
                    response.network_error
                    or response.description
                    or f"HTTP {response.status_code}"
                )
                raise SessionError(f"Could not obtain a session key: {reason}", response)
            if self._config.session_warmup:
                # Docs: a new session can take up to ~30s to become live.
                await self._sleep(self._config.session_warmup)
            self._session = str(session_id)
            self._expires_at = self._clock() + self._config.session_ttl
            return self._session

    def invalidate(self, session: str | None = None) -> None:
        """Drop the cached session (only if it is still ``session``, when given)."""
        if session is None or session == self._session:
            self._session = None
