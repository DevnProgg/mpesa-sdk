"""Transaction types and lifecycle statuses."""

from __future__ import annotations

from enum import Enum


class TransactionType(str, Enum):
    """Operations the SDK can initialise. Add new ones in ``operations.py``."""

    C2B_SINGLE_STAGE = "c2b_single_stage"
    C2B_SINGLE_STAGE_ASYNC = "c2b_single_stage_async"


class TransactionStatus(str, Enum):
    QUEUED = "queued"  # accepted by the SDK, waiting for a Celery worker
    RETRYING = "retrying"  # an attempt failed transiently, another is scheduled
    PENDING = "pending"  # accepted by M-Pesa, final result arrives by callback
    SUCCEEDED = "succeeded"  # terminal
    FAILED = "failed"  # terminal, definitely did not complete
    UNKNOWN = "unknown"  # terminal, outcome unclear: reconcile before re-charging

    @property
    def is_final(self) -> bool:
        return self in _FINAL


_FINAL = frozenset(
    {TransactionStatus.SUCCEEDED, TransactionStatus.FAILED, TransactionStatus.UNKNOWN}
)
