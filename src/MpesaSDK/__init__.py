"""MpesaSDK: auditable, retrying M-Pesa OpenAPI payments.

import MpesaSDK as mp

provider = mp.config({"market": mp.markets.LESOTHO, "api-key": "...", "public-key": "..."})
response = await provider.initialize_transaction(
    type=mp.transactions.C2B_SINGLE_STAGE, payload={...}
)
"""

from . import markets
from .audit import AttemptRecord, AuditTrail
from .callbacks import CallbackResult, parse_callback
from .errors import (
    ConfigError,
    CryptoError,
    MpesaError,
    SessionError,
    TransactionError,
    ValidationError,
)
from .provider import MpesaProvider, config
from .response import TransactionFailure, TransactionResponse
from .settings import MpesaConfig, RetryStrategy
from .transactions import TransactionStatus, TransactionType

transactions = TransactionType  # mp.transactions.C2B_SINGLE_STAGE_ASYNC
status = TransactionStatus

__version__ = "0.1.0"

__all__ = [
    "AttemptRecord",
    "AuditTrail",
    "CallbackResult",
    "ConfigError",
    "CryptoError",
    "MpesaConfig",
    "MpesaError",
    "MpesaProvider",
    "RetryStrategy",
    "SessionError",
    "TransactionError",
    "TransactionFailure",
    "TransactionResponse",
    "TransactionStatus",
    "TransactionType",
    "ValidationError",
    "config",
    "markets",
    "parse_callback",
    "status",
    "transactions",
]
