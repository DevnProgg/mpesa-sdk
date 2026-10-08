"""Turn an M-Pesa response into a decision: succeed, retry, or stop.

Rules (driven by the documented response codes):

* ``INS-0``                      -> success
* ``INS-1`` / ``INS-9``, network errors, HTTP 5xx -> retry (outcome *ambiguous*)
* HTTP 429, gateway 401/403 with no OpenAPI body  -> retry (never reached the platform)
* ``INS-10`` duplicate           -> never retried; after a retry it means an earlier
                                    attempt landed, so the outcome is UNKNOWN
* everything else (validation, limits, balance, bad MSISDN ...) -> terminal failure
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .http import ApiResponse


class Disposition(str, Enum):
    SUCCESS = "success"
    RETRY = "retry"
    TERMINAL = "terminal"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class Verdict:
    disposition: Disposition
    code: str
    message: str
    ambiguous: bool = False  # the platform may have processed the request
    refresh_session: bool = False


KNOWN_CODES = {
    "INS-0": "Request processed successfully",
    "INS-1": "Internal Error",
    "INS-6": "Transaction Failed",
    "INS-9": "Request timeout",
    "INS-10": "Duplicate Transaction",
    "INS-13": "Invalid Shortcode Used",
    "INS-15": "Invalid Amount Used",
    "INS-17": "Invalid Transaction Reference. Length Should Be Between 1 and 20.",
    "INS-20": "Not All Parameters Provided. Please try again.",
    "INS-21": "Parameter validations failed. Please try again.",
    "INS-26": "Invalid Currency Used",
    "INS-28": "Invalid ThirdPartyConversationID Used",
    "INS-30": "Invalid Purchased Items Description Used",
    "INS-31": "Invalid Payment Items Description Used",  # B2C
    "INS-989": "Session Creation Failed",
    "INS-990": "Customer Transaction Value Limit Breached",
    "INS-991": "Customer Transaction Count Limit Breached",
    "INS-992": "Multiple Limits Breached",
    "INS-993": "Organization Transaction Count Limit Breached",
    "INS-994": "Organization Transaction Value Limit Breached",
    "INS-995": "API Single Transaction Limit Breached",
    "INS-996": "API Being Used Outside Of Usage Time",
    "INS-997": "API Not Enabled",
    "INS-998": "Invalid Market",
    "INS-999": "Invalid Use Case",  # observed in the sandbox; not in the published tables
    "INS-2006": "Insufficient balance",
    "INS-2051": "MSISDN invalid.",
}

_RETRYABLE_CODES = frozenset({"INS-1", "INS-9"})
# Limits are enforced per session: a fresh session key is needed once they trip.
_SESSION_LIMIT_CODES = frozenset({"INS-992", "INS-993", "INS-994"})


def classify(response: ApiResponse) -> Verdict:
    if response.network_error:
        return Verdict(Disposition.RETRY, "NETWORK_ERROR", response.network_error, ambiguous=True)

    status = response.status_code or 0
    code = response.code
    if code is None:
        if status in (401, 403):
            return Verdict(
                Disposition.RETRY,
                "AUTH_FAILED",
                f"HTTP {status} from gateway",
                refresh_session=True,
            )
        if status == 429:
            return Verdict(Disposition.RETRY, "RATE_LIMITED", "HTTP 429 from gateway")
        if status >= 500:
            return Verdict(
                Disposition.RETRY, "SERVER_ERROR", f"HTTP {status} from gateway", ambiguous=True
            )
        return Verdict(Disposition.TERMINAL, "UNEXPECTED_RESPONSE", f"Unexpected HTTP {status}")

    message = response.description or KNOWN_CODES.get(code, "Unrecognised response code")
    if code == "INS-0":
        return Verdict(Disposition.SUCCESS, code, message)
    if code == "INS-10":
        return Verdict(Disposition.DUPLICATE, code, message)
    if code in _RETRYABLE_CODES:
        return Verdict(Disposition.RETRY, code, message, ambiguous=True)
    return Verdict(
        Disposition.TERMINAL, code, message, refresh_session=code in _SESSION_LIMIT_CODES
    )
