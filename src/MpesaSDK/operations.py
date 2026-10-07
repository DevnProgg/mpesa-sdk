"""Registry of supported operations. To add one, add a payload class and an entry here."""

from __future__ import annotations

from dataclasses import dataclass

from .payloads import C2BSingleStagePayload
from .transactions import TransactionType


@dataclass(frozen=True, slots=True)
class Operation:
    type: TransactionType
    path: str  # endpoint path after /ipg/v2/<market>/
    payload_cls: type[C2BSingleStagePayload]
    awaits_callback: bool  # final result is delivered to your listener, not in the response


OPERATIONS: dict[TransactionType, Operation] = {
    TransactionType.C2B_SINGLE_STAGE: Operation(
        TransactionType.C2B_SINGLE_STAGE, "c2bPayment/singleStage/", C2BSingleStagePayload, False
    ),
    TransactionType.C2B_SINGLE_STAGE_ASYNC: Operation(
        TransactionType.C2B_SINGLE_STAGE_ASYNC,
        "c2bPayment/singleStage/",
        C2BSingleStagePayload,
        True,
    ),
}
