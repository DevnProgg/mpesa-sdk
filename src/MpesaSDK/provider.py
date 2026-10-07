"""The public entry point: ``MpesaProvider`` and the ``config()`` factory."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from ._aio import run_sync
from .audit import AuditTrail, jsonable, utc_now
from .callbacks import CallbackResult, parse_callback
from .engine import StepResult, TransactionEngine
from .errors import ConfigError, TransactionError, ValidationError
from .http import MpesaHttpClient
from .operations import OPERATIONS, Operation
from .response import TransactionFailure, TransactionResponse
from .retry import RetryPolicy
from .session import SessionManager
from .settings import MpesaConfig, parse_config
from .transactions import TransactionStatus, TransactionType

logger = logging.getLogger("MpesaSDK")
logger.addHandler(logging.NullHandler())


class MpesaProvider:
    def __init__(
        self, config: MpesaConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        client = MpesaHttpClient(config, transport)
        self._sessions = SessionManager(config, client)
        self._engine = TransactionEngine(client, self._sessions, RetryPolicy(config.retry))
        self._queue = None
        if config.concurrency:
            from .tasks import CeleryQueue  # optional dependency, imported lazily

            self._queue = CeleryQueue(config, self._run_queued_step)

    @property
    def config(self) -> MpesaConfig:
        return self._config

    @property
    def celery_app(self):
        """The Celery app. Expose it in your module and point ``celery -A`` at it."""
        if self._queue is None:
            raise ConfigError("celery_app is only available when concurrency is True")
        return self._queue.app

    # ------------------------------------------------------------------ initialise

    async def initialize_transaction(
        self, type: TransactionType | str, payload: Mapping[str, Any]
    ) -> TransactionResponse:
        """Validate, then run (inline) or enqueue (Celery) a transaction.

        Never raises for payment problems: inspect ``response.ok`` / ``response.err``.
        With ``concurrency`` on, the returned status is QUEUED; use :meth:`wait_for`.
        """
        operation = OPERATIONS[TransactionType(type)]
        try:
            parsed = operation.payload_cls.from_mapping(payload, self._config.market)
        except ValidationError as exc:
            return self._rejected(operation, payload, exc)

        audit = AuditTrail(
            transaction_id=parsed.transaction_id,
            transaction_type=operation.type.value,
            amount=parsed.amount,
            currency=parsed.currency,
            recipient=parsed.recipient,
            payer=parsed.payer,
            timestamp=utc_now(),
            status=TransactionStatus.QUEUED,
            request_payload=parsed.to_request(),
        )
        if self._queue is not None:
            return await self._enqueue(operation, audit)

        step = await self._engine.run(operation, audit)
        response = self._to_response(step)
        await self._notify(response)
        return response

    initializeTransaction = initialize_transaction  # camelCase alias

    async def initialize_many(
        self, requests: Iterable[tuple[TransactionType | str, Mapping[str, Any]]]
    ) -> list[TransactionResponse]:
        """Initialise several transactions with at most ``max_workers`` in flight."""
        gate = asyncio.Semaphore(self._config.max_workers)

        async def one(kind: TransactionType | str, payload: Mapping[str, Any]):
            async with gate:
                return await self.initialize_transaction(kind, payload)

        return list(await asyncio.gather(*(one(k, p) for k, p in requests)))

    # --------------------------------------------------------------- queue results

    async def get_result(self, transaction_id: str) -> TransactionResponse | None:
        """Final response of a queued transaction, or ``None`` if still running."""
        queue = self._require_queue()
        data = await asyncio.to_thread(queue.fetch, transaction_id)
        return None if data is None else TransactionResponse.from_dict(data)

    async def wait_for(
        self, transaction_id: str, *, timeout: float | None = None, poll_interval: float = 0.5
    ) -> TransactionResponse:
        """Poll until a queued transaction reaches a final state (or PENDING for async)."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while (result := await self.get_result(transaction_id)) is None:
            if deadline is not None and loop.time() >= deadline:
                raise TimeoutError(f"Transaction {transaction_id} not finished after {timeout}s")
            await asyncio.sleep(poll_interval)
        return result

    # ------------------------------------------------------------------- callbacks

    @staticmethod
    def handle_callback(body: Mapping[str, Any] | str | bytes) -> CallbackResult:
        """Parse the async result M-Pesa POSTs to your listener."""
        if isinstance(body, (str, bytes)):
            body = json.loads(body)
        return parse_callback(body)

    # ------------------------------------------------------------------- internals

    def _require_queue(self):
        if self._queue is None:
            raise ConfigError("Queued results need concurrency=True")
        return self._queue

    def _to_response(self, step: StepResult, task_id: str | None = None) -> TransactionResponse:
        return TransactionResponse(step.audit.status, step.audit, step.failure, task_id)

    def _rejected(
        self, operation: Operation, raw: Mapping[str, Any], exc: ValidationError
    ) -> TransactionResponse:
        peek = operation.payload_cls.peek(raw)
        audit = AuditTrail(
            transaction_id=peek["transaction_id"] or uuid.uuid4().hex,
            transaction_type=operation.type.value,
            amount=peek["amount"],
            currency=self._config.market.currency,
            recipient=peek["recipient"],
            payer=peek["payer"],
            timestamp=utc_now(),
            status=TransactionStatus.FAILED,
            request_payload=jsonable(dict(raw)),
            completed_at=utc_now(),
        )
        failure = TransactionFailure("VALIDATION_ERROR", str(exc))
        return TransactionResponse(TransactionStatus.FAILED, audit, failure)

    async def _enqueue(self, operation: Operation, audit: AuditTrail) -> TransactionResponse:
        spec = {"type": operation.type.value, "audit": audit.to_dict()}
        try:
            await asyncio.to_thread(self._queue.enqueue, spec, audit.transaction_id)
        except Exception as exc:  # broker down, serialisation error, ...
            logger.exception("Could not enqueue transaction %s", audit.transaction_id)
            audit.status = TransactionStatus.FAILED  # nothing was sent to M-Pesa
            audit.completed_at = utc_now()
            failure = TransactionFailure(
                "QUEUE_UNAVAILABLE", f"Could not enqueue transaction: {exc}", retryable=True
            )
            response = TransactionResponse(TransactionStatus.FAILED, audit, failure)
            await self._notify(response)
            return response
        return TransactionResponse(TransactionStatus.QUEUED, audit, None, audit.transaction_id)

    def _run_queued_step(self, spec: dict[str, Any], task_id: str | None):
        """Worker side: one attempt, then either a final response or a retry request."""
        from .tasks import QueuedStep

        operation = OPERATIONS[TransactionType(spec["type"])]
        audit = AuditTrail.from_dict(spec["audit"])
        try:
            step = run_sync(self._engine.step(operation, audit))
        except Exception as exc:  # a bug must still leave an audit trail
            logger.exception("Unexpected error processing %s", audit.transaction_id)
            audit.status = TransactionStatus.UNKNOWN
            audit.completed_at = utc_now()
            failure = TransactionFailure(
                "INTERNAL_ERROR", f"Unexpected SDK error: {exc}", needs_reconciliation=True
            )
            step = StepResult(audit, failure)

        if step.retry_delay is not None:
            return QueuedStep(
                next_spec={**spec, "audit": step.audit.to_dict()}, retry_delay=step.retry_delay
            )
        response = self._to_response(step, task_id)
        run_sync(self._notify(response))
        return QueuedStep(response=response.to_dict())

    async def _notify(self, response: TransactionResponse) -> None:
        handler = self._config.audit_handler
        if handler is None:
            return
        try:
            result = handler(response)
            if inspect.isawaitable(result):
                await result
        except Exception:  # an audit-store outage must not change the payment outcome
            logger.exception("audit-handler failed for %s", response.transaction_id)


def config(
    settings: Mapping[str, Any] | MpesaConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MpesaProvider:
    """Build a provider from a settings dict. ``transport`` is a test hook."""
    return MpesaProvider(parse_config(settings), transport=transport)


__all__ = ["MpesaProvider", "TransactionError", "config"]
