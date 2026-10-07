"""The audit trail attached to every transaction response, good or bad.

``AuditTrail.log`` is a JSON-safe ``dict`` that can be stored as-is. It never contains
the API key, session key or any other credential.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .transactions import TransactionStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def jsonable(value: Any) -> Any:
    """Round-trip through JSON so the audit log only ever holds plain JSON types."""
    return json.loads(json.dumps(value, default=str))


@dataclass(slots=True)
class AttemptRecord:
    number: int
    timestamp: str
    http_status: int | None
    response_code: str | None
    disposition: str
    message: str
    ambiguous: bool  # may the request have reached M-Pesa without a definite answer?
    duration_ms: int


@dataclass(slots=True)
class AuditTrail:
    transaction_id: str  # == input_ThirdPartyConversationID, the idempotency key
    transaction_type: str
    amount: str | None
    currency: str | None
    recipient: str | None  # who receives the funds (C2B: business shortcode)
    payer: str | None
    timestamp: str  # when the transaction was initialised (UTC, ISO-8601)
    status: TransactionStatus
    retry_count: int = 0
    mpesa_api_response: dict[str, Any] | None = None  # last response body from M-Pesa
    request_payload: dict[str, Any] = field(default_factory=dict)  # body sent to M-Pesa
    attempts: list[AttemptRecord] = field(default_factory=list)
    conversation_id: str | None = None
    mpesa_transaction_id: str | None = None
    callback: dict[str, Any] | None = None  # async result, once received
    completed_at: str | None = None

    @property
    def log(self) -> dict[str, Any]:
        """JSON-safe dict, ready to insert into your audit table."""
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditTrail:
        data = dict(data)
        data["status"] = TransactionStatus(data["status"])
        data["attempts"] = [AttemptRecord(**a) for a in data.get("attempts", [])]
        return cls(**data)

    @property
    def any_ambiguous_attempt(self) -> bool:
        return any(a.ambiguous for a in self.attempts)
