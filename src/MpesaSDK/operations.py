"""Registry of supported operations. To add one, add a payload class and an entry here."""

from __future__ import annotations

from dataclasses import dataclass

from .payloads import (
    B2BSingleStagePayload,
    B2CSingleStagePayload,
    C2BSingleStagePayload,
    ReversalPayload,
    SingleStagePayload,
)
from .transactions import TransactionType


@dataclass(frozen=True, slots=True)
class Operation:
    type: TransactionType
    path: str  # endpoint path after /ipg/v2/<market>/
    payload_cls: type[SingleStagePayload]
    awaits_callback: bool  # final result is delivered to your listener, not in the response
    method: str = "POST"  # HTTP method


def _both(sync: TransactionType, async_: TransactionType, path: str, cls, method: str = "POST"):
    return {
        sync: Operation(sync, path, cls, False, method),
        async_: Operation(async_, path, cls, True, method),
    }


OPERATIONS: dict[TransactionType, Operation] = {
    **_both(
        TransactionType.C2B_SINGLE_STAGE,
        TransactionType.C2B_SINGLE_STAGE_ASYNC,
        "c2bPayment/singleStage/",
        C2BSingleStagePayload,
    ),
    **_both(
        TransactionType.B2C_SINGLE_STAGE,
        TransactionType.B2C_SINGLE_STAGE_ASYNC,
        "b2cPayment/",
        B2CSingleStagePayload,
    ),
    **_both(
        TransactionType.B2B_SINGLE_STAGE,
        TransactionType.B2B_SINGLE_STAGE_ASYNC,
        "b2bPayment/",
        B2BSingleStagePayload,
    ),
    **_both(
        TransactionType.REVERSAL,
        TransactionType.REVERSAL_ASYNC,
        "reversal/",
        ReversalPayload,
        method="PUT",
    ),
}
