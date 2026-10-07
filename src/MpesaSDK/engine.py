"""Executes one attempt of a transaction and decides what happens next.

``step`` is the single unit of work. Inline mode loops over it with ``asyncio.sleep``;
Celery mode runs one step per task execution and lets Celery schedule the retry, so a
backing-off transaction never occupies a worker slot.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .audit import AttemptRecord, AuditTrail, utc_now
from .codes import Disposition, Verdict, classify
from .errors import SessionError
from .http import MpesaHttpClient
from .operations import Operation
from .response import TransactionFailure
from .retry import RetryPolicy
from .session import SessionManager
from .transactions import TransactionStatus


@dataclass(frozen=True, slots=True)
class StepResult:
    audit: AuditTrail
    failure: TransactionFailure | None = None
    retry_delay: float | None = None  # not None => another attempt must be scheduled


class TransactionEngine:
    def __init__(
        self,
        client: MpesaHttpClient,
        sessions: SessionManager,
        policy: RetryPolicy,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._policy = policy
        self._sleep = sleep

    async def run(self, operation: Operation, audit: AuditTrail) -> StepResult:
        """Inline execution: attempt, back off, repeat until a final state."""
        while True:
            result = await self.step(operation, audit)
            if result.retry_delay is None:
                return result
            await self._sleep(result.retry_delay)

    async def step(self, operation: Operation, audit: AuditTrail) -> StepResult:
        started = time.perf_counter()
        session: str | None = None
        status_code: int | None = None
        body_code: str | None = None
        try:
            session = await self._sessions.get()
        except SessionError as exc:
            # No payment request was sent, so retrying is always safe.
            verdict = Verdict(Disposition.RETRY, "SESSION_ERROR", str(exc))
        else:
            response = await self._client.post(operation.path, session, audit.request_payload)
            verdict = classify(response)
            status_code, body_code = response.status_code, response.code
            if response.body:
                audit.mpesa_api_response = response.body

        audit.attempts.append(
            AttemptRecord(
                number=len(audit.attempts) + 1,
                timestamp=utc_now(),
                http_status=status_code,
                response_code=body_code or verdict.code,
                disposition=verdict.disposition.value,
                message=verdict.message,
                ambiguous=verdict.ambiguous,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        if verdict.refresh_session:
            self._sessions.invalidate(session)
        return self._decide(operation, audit, verdict, status_code)

    def _decide(
        self, operation: Operation, audit: AuditTrail, verdict: Verdict, http_status: int | None
    ) -> StepResult:
        d = verdict.disposition

        if d is Disposition.SUCCESS:
            body = audit.mpesa_api_response or {}
            audit.conversation_id = body.get("output_ConversationID")
            audit.mpesa_transaction_id = body.get("output_TransactionID")
            if operation.awaits_callback:
                audit.status = TransactionStatus.PENDING
            else:
                self._finish(audit, TransactionStatus.SUCCEEDED)
            return StepResult(audit)

        if d is Disposition.RETRY and self._policy.should_retry(audit.retry_count):
            audit.retry_count += 1
            audit.status = TransactionStatus.RETRYING
            return StepResult(audit, retry_delay=self._policy.delay(audit.retry_count))

        if d is Disposition.DUPLICATE and audit.any_ambiguous_attempt:
            # An earlier attempt reached M-Pesa but we never saw its answer.
            return self._unknown(
                audit,
                verdict,
                http_status,
                "An earlier attempt was received by M-Pesa (duplicate on retry). "
                "Query the transaction status before charging again.",
            )

        if d is Disposition.RETRY:  # retries exhausted
            if audit.any_ambiguous_attempt:
                return self._unknown(
                    audit,
                    verdict,
                    http_status,
                    f"Retries exhausted ({audit.retry_count}); the outcome is unknown: "
                    f"{verdict.message}. Query the transaction status before charging again.",
                )
            return self._fail(
                audit,
                verdict,
                http_status,
                f"Retries exhausted: {verdict.message}",
                retryable=True,
            )

        return self._fail(audit, verdict, http_status, verdict.message)

    @staticmethod
    def _finish(audit: AuditTrail, status: TransactionStatus) -> None:
        audit.status = status
        audit.completed_at = utc_now()

    def _fail(
        self,
        audit: AuditTrail,
        verdict: Verdict,
        http_status: int | None,
        message: str,
        *,
        retryable: bool = False,
    ) -> StepResult:
        self._finish(audit, TransactionStatus.FAILED)
        failure = TransactionFailure(
            verdict.code, message, retryable=retryable, http_status=http_status
        )
        return StepResult(audit, failure)

    def _unknown(
        self, audit: AuditTrail, verdict: Verdict, http_status: int | None, message: str
    ) -> StepResult:
        self._finish(audit, TransactionStatus.UNKNOWN)
        failure = TransactionFailure(
            verdict.code,
            message,
            retryable=False,
            http_status=http_status,
            needs_reconciliation=True,
        )
        return StepResult(audit, failure)
