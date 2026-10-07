"""What ``initialize_transaction`` returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .audit import AuditTrail
from .errors import TransactionError
from .transactions import TransactionStatus


@dataclass(frozen=True, slots=True)
class TransactionFailure:
    code: str  # e.g. "INS-2006", "NETWORK_ERROR", "VALIDATION_ERROR"
    message: str
    # Keyword-only so a positional mix-up can never silently shift values.
    retryable: bool = field(default=False, kw_only=True)  # a fresh transaction may succeed later
    http_status: int | None = field(default=None, kw_only=True)
    # True when money may have moved: reconcile before charging again.
    needs_reconciliation: bool = field(default=False, kw_only=True)


@dataclass(frozen=True, slots=True)
class TransactionResponse:
    status: TransactionStatus
    audit: AuditTrail
    err: TransactionFailure | None = None
    task_id: str | None = None  # set when processed by a Celery worker

    @property
    def ok(self) -> bool:
        """True when the transaction is succeeded, queued, or accepted and awaiting callback.

        ``ok`` does *not* mean "money moved" for QUEUED/PENDING: check ``status``/``is_final``.
        """
        return self.status not in (TransactionStatus.FAILED, TransactionStatus.UNKNOWN)

    @property
    def is_final(self) -> bool:
        return self.status.is_final

    @property
    def transaction_id(self) -> str:
        return self.audit.transaction_id

    def raise_for_error(self) -> None:
        if self.err is not None:
            raise TransactionError(self.err.message, code=self.err.code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "audit": self.audit.to_dict(),
            "err": asdict(self.err) if self.err else None,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransactionResponse:
        err = data.get("err")
        return cls(
            status=TransactionStatus(data["status"]),
            audit=AuditTrail.from_dict(data["audit"]),
            err=TransactionFailure(**err) if err else None,
            task_id=data.get("task_id"),
        )
