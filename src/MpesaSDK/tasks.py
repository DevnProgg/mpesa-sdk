"""Celery/Redis transport. Imported only when ``concurrency`` is enabled.

Security: the task message carries the audit trail (payload, MSISDN, amount) but never
credentials; workers build their own provider from the same config as your app.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError
from .settings import MpesaConfig

try:
    from celery import Celery
except ImportError as exc:  # pragma: no cover
    raise ConfigError("concurrency=True needs Celery: pip install 'mpesa-sdk[queue]'") from exc

TASK_NAME = "MpesaSDK.execute_transaction"


@dataclass(frozen=True, slots=True)
class QueuedStep:
    """Outcome of one worker run: either a final response dict or a retry request."""

    response: dict[str, Any] | None = None
    next_spec: dict[str, Any] | None = None
    retry_delay: float | None = None


class CeleryQueue:
    def __init__(
        self, config: MpesaConfig, process_step: Callable[[dict, str | None], QueuedStep]
    ):
        app = Celery("MpesaSDK", broker=config.redis_url, backend=config.redis_url)
        app.conf.update(
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
            task_acks_late=True,  # a crashed worker's task is redelivered; M-Pesa dedupes it
            task_reject_on_worker_lost=True,
            worker_prefetch_multiplier=1,
            worker_concurrency=config.max_workers,
            task_default_queue=config.queue,
            result_expires=86_400,
            broker_connection_retry_on_startup=True,
        )
        app.conf.update(dict(config.celery_options))
        self.app = app

        @app.task(bind=True, name=TASK_NAME, max_retries=None, shared=False)
        def execute_transaction(task, spec: dict) -> dict:
            step = process_step(spec, task.request.id)
            if step.retry_delay is not None:
                # Same task id, so callers can keep polling the original id.
                raise task.retry(args=(step.next_spec,), countdown=step.retry_delay)
            return step.response  # type: ignore[return-value]

        self._task = execute_transaction

    def enqueue(self, spec: dict[str, Any], task_id: str) -> None:
        self._task.apply_async(args=(spec,), task_id=task_id)

    def fetch(self, task_id: str) -> dict[str, Any] | None:
        """Final response dict, or ``None`` if the task is not finished yet."""
        result = self.app.AsyncResult(task_id)
        if not result.ready():
            return None
        if result.failed():
            raise RuntimeError(f"Worker task failed: {result.result!r}")
        return result.result
