"""Helpers for the asynchronous-result listener you host for ``*_ASYNC`` transactions.

M-Pesa POSTs the final result to your listener; you must reply with ``result.ack()``.
Verify the caller yourself (e.g. IP allow-list): the API docs define no signature.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .audit import AuditTrail, jsonable, utc_now
from .errors import ValidationError
from .transactions import TransactionStatus

_REQUIRED = (
    "input_OriginalConversationID",
    "input_ResultCode",
    "input_ThirdPartyConversationID",
)


@dataclass(frozen=True, slots=True)
class CallbackResult:
    original_conversation_id: str
    transaction_id: str | None
    result_code: str
    result_desc: str
    third_party_conversation_id: str
    raw: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.result_code == "INS-0"

    def ack(self) -> dict[str, str]:
        """The body your listener must return to close the session."""
        return {
            "output_OriginalConversationID": self.original_conversation_id,
            "output_ResponseCode": "0",
            "output_ResponseDesc": "Successfully Accepted Result",
            "output_ThirdPartyConversationID": self.third_party_conversation_id,
        }

    def apply_to(self, audit: AuditTrail) -> AuditTrail:
        """Return a copy of ``audit`` updated with this final result."""
        if audit.transaction_id != self.third_party_conversation_id:
            raise ValueError("Callback does not belong to this audit trail")
        return replace(
            audit,
            status=TransactionStatus.SUCCEEDED if self.ok else TransactionStatus.FAILED,
            callback=self.raw,
            mpesa_transaction_id=self.transaction_id or audit.mpesa_transaction_id,
            completed_at=utc_now(),
        )


def parse_callback(body: Mapping[str, Any]) -> CallbackResult:
    missing = {k: "is required" for k in _REQUIRED if not body.get(k)}
    if missing:
        raise ValidationError(missing)
    return CallbackResult(
        original_conversation_id=str(body["input_OriginalConversationID"]),
        transaction_id=body.get("input_TransactionID"),
        result_code=str(body["input_ResultCode"]),
        result_desc=str(body.get("input_ResultDesc", "")),
        third_party_conversation_id=str(body["input_ThirdPartyConversationID"]),
        raw=jsonable(dict(body)),
    )
