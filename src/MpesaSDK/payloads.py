"""Transaction payload validation.

Developers send friendly keys (``amount``, ``customer_msisdn`` ...). The raw OpenAPI
names (``input_Amount`` ...) are accepted too. Country and currency come from the market.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ValidationError
from .markets import Market

_AMOUNT = re.compile(r"^\d*\.?\d+$")
_SHORTCODE = re.compile(r"^[0-9A-Za-z]{4,12}$")
_REFERENCE = re.compile(r"^\w{1,20}$", re.ASCII)
_THIRD_PARTY_ID = re.compile(r"^\w{1,40}$", re.ASCII)

# squashed key (lower-case, no "-", "_" or "input" prefix) -> canonical field name
_ALIASES = {
    "amount": "amount",
    "customermsisdn": "customer_msisdn",
    "msisdn": "customer_msisdn",
    "serviceprovidercode": "service_provider_code",
    "shortcode": "service_provider_code",
    "transactionreference": "transaction_reference",
    "reference": "transaction_reference",
    "purchaseditemsdesc": "purchased_items_desc",
    "description": "purchased_items_desc",
    "thirdpartyconversationid": "third_party_conversation_id",
    "idempotencykey": "third_party_conversation_id",
    "country": "country",
    "currency": "currency",
}


def _squash(key: str) -> str:
    squashed = key.strip().lower().replace("-", "").replace("_", "")
    return squashed.removeprefix("input")


def canonicalise(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Map caller keys to canonical field names. Returns ``(fields, errors)``."""
    found: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, value in raw.items():
        canonical = _ALIASES.get(_squash(str(key)))
        if canonical is None:
            errors[str(key)] = "unknown field"
        elif canonical in found:
            errors[canonical] = "provided more than once"
        else:
            found[canonical] = value
    return found, errors


def _parse_amount(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("must be a number")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError("must be a number") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("must be greater than zero")
    text = format(number, "f")
    if not _AMOUNT.match(text):
        raise ValueError("must be a plain decimal such as 10 or 10.50")
    return text


def _match(pattern: re.Pattern[str], message: str):
    def check(value: Any) -> str:
        if not isinstance(value, str) or not pattern.match(value):
            raise ValueError(message)
        return value

    return check


def _description(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("must be 1-256 characters")
    return value


_REQUIRED = {
    "amount": _parse_amount,
    "service_provider_code": _match(_SHORTCODE, "must be 4-12 letters/digits"),
    "transaction_reference": _match(_REFERENCE, "must be 1-20 letters, digits or underscores"),
    "purchased_items_desc": _description,
}


@dataclass(frozen=True, slots=True)
class C2BSingleStagePayload:
    """Validated payload for the C2B single-stage endpoint."""

    amount: str
    customer_msisdn: str
    service_provider_code: str
    transaction_reference: str
    purchased_items_desc: str
    third_party_conversation_id: str
    country: str
    currency: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], market: Market) -> C2BSingleStagePayload:
        fields, errors = canonicalise(raw)
        clean: dict[str, str] = {}

        for name, check in _REQUIRED.items():
            if name not in fields:
                errors[name] = "is required"
                continue
            try:
                clean[name] = check(fields[name])
            except ValueError as exc:
                errors[name] = str(exc)

        msisdn = fields.get("customer_msisdn")
        if msisdn is None:
            errors["customer_msisdn"] = "is required"
        elif not isinstance(msisdn, str) or not market.is_valid_msisdn(msisdn):
            errors["customer_msisdn"] = f"must be digits only and valid for {market.name}"
        else:
            clean["customer_msisdn"] = msisdn

        # Idempotency key: reused across retries so M-Pesa can detect duplicates.
        tp_id = fields.get("third_party_conversation_id") or uuid.uuid4().hex
        if not isinstance(tp_id, str) or not _THIRD_PARTY_ID.match(tp_id):
            errors["third_party_conversation_id"] = "must be 1-40 letters, digits or underscores"
        else:
            clean["third_party_conversation_id"] = tp_id

        for name, expected in (("country", market.country), ("currency", market.currency)):
            given = fields.get(name, expected)
            if given != expected:
                errors[name] = f"must be {expected!r} for market {market.name}"
            clean[name] = expected

        if errors:
            raise ValidationError(errors)
        return cls(**clean)

    @staticmethod
    def peek(raw: Mapping[str, Any]) -> dict[str, str | None]:
        """Best-effort amount/recipient/payer for the audit trail of a rejected payload."""
        fields, _ = canonicalise(raw)

        def text(name: str) -> str | None:
            value = fields.get(name)
            return None if value is None else str(value)

        return {
            "amount": text("amount"),
            "recipient": text("service_provider_code"),
            "payer": text("customer_msisdn"),
            "transaction_id": text("third_party_conversation_id"),
        }

    @property
    def recipient(self) -> str:
        """The party receiving the funds (for C2B: the business shortcode)."""
        return self.service_provider_code

    @property
    def payer(self) -> str:
        return self.customer_msisdn

    @property
    def transaction_id(self) -> str:
        return self.third_party_conversation_id

    def to_request(self) -> dict[str, str]:
        """The exact JSON body sent to M-Pesa."""
        return {
            "input_Amount": self.amount,
            "input_Country": self.country,
            "input_Currency": self.currency,
            "input_CustomerMSISDN": self.customer_msisdn,
            "input_ServiceProviderCode": self.service_provider_code,
            "input_ThirdPartyConversationID": self.third_party_conversation_id,
            "input_TransactionReference": self.transaction_reference,
            "input_PurchasedItemsDesc": self.purchased_items_desc,
        }
