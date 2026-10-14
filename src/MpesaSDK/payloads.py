"""Transaction payload validation for the single-stage C2B, B2C and B2B endpoints.

Each payload class declares its fields, the OpenAPI key for each, and which field is the
recipient and which the payer; validation and serialisation are shared.

Developers send friendly keys (``amount``, ``customer_msisdn`` ...). The raw OpenAPI
names (``input_Amount`` ...) are accepted too. Country and currency come from the market.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

from .errors import ValidationError
from .markets import Market

_AMOUNT = re.compile(r"^\d*\.?\d+$")
_SHORTCODE = re.compile(r"^[0-9A-Za-z]{4,12}$")
_REFERENCE = re.compile(r"^\w{1,20}$", re.ASCII)
_THIRD_PARTY_ID = re.compile(r"^\w{1,40}$", re.ASCII)

# squashed key (lower-case, no "-", "_" or "input" prefix) -> canonical field name
_COMMON_ALIASES = {
    "amount": "amount",
    "transactionreference": "transaction_reference",
    "reference": "transaction_reference",
    "thirdpartyconversationid": "third_party_conversation_id",
    "idempotencykey": "third_party_conversation_id",
    "country": "country",
    "currency": "currency",
    "description": "items_desc",
}


def _squash(key: str) -> str:
    squashed = key.strip().lower().replace("-", "").replace("_", "")
    return squashed.removeprefix("input")


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
    "transaction_reference": _match(_REFERENCE, "must be 1-20 letters, digits or underscores"),
    "items_desc": _description,
}


class SingleStagePayload:
    """Base for validated single-stage payloads. Subclasses are frozen dataclasses."""

    __slots__ = ()

    DESC_FIELD: ClassVar[str]  # public name of the description field (used in error messages)
    DESC_REQUEST_KEY: ClassVar[str]  # OpenAPI body key for the description
    EXTRA_ALIASES: ClassVar[Mapping[str, str]] = {}  # squashed key -> field, per type
    REQUEST_KEYS: ClassVar[Mapping[str, str]]  # field -> OpenAPI body key (defines the body)
    CODE_FIELDS: ClassVar[tuple[str, ...]] = ()  # shortcode-style fields
    MSISDN_FIELD: ClassVar[str | None] = None  # field holding a customer phone number
    RECIPIENT_FIELD: ClassVar[str]  # who receives the funds
    PAYER_FIELD: ClassVar[str]  # who the funds are taken from

    @classmethod
    def _aliases(cls) -> dict[str, str]:
        return {
            **_COMMON_ALIASES,
            **cls.EXTRA_ALIASES,
            _squash(cls.DESC_REQUEST_KEY): "items_desc",
        }

    @classmethod
    def _canonicalise(cls, raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """Map caller keys to canonical field names. Returns ``(fields, errors)``."""
        aliases = cls._aliases()
        found: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, value in raw.items():
            canonical = aliases.get(_squash(str(key)))
            if canonical is None:
                errors[str(key)] = "unknown field"
            elif canonical in found:
                errors[canonical] = "provided more than once"
            else:
                found[canonical] = value
        return found, errors

    @classmethod
    def _cross_validate(cls, clean: dict[str, str], errors: dict[str, str]) -> None:
        """Hook for rules involving several fields."""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], market: Market) -> SingleStagePayload:
        fields, errors = cls._canonicalise(raw)
        clean: dict[str, str] = {}

        validators = {
            **_REQUIRED,
            **{
                name: _match(_SHORTCODE, "must be 4-12 letters/digits") for name in cls.CODE_FIELDS
            },
        }
        for name, check in validators.items():
            if name not in fields:
                errors[name] = "is required"
                continue
            try:
                clean[name] = check(fields[name])
            except ValueError as exc:
                errors[name] = str(exc)

        if cls.MSISDN_FIELD:
            msisdn = fields.get(cls.MSISDN_FIELD)
            if msisdn is None:
                errors[cls.MSISDN_FIELD] = "is required"
            elif not isinstance(msisdn, str) or not market.is_valid_msisdn(msisdn):
                errors[cls.MSISDN_FIELD] = f"must be digits only and valid for {market.name}"
            else:
                clean[cls.MSISDN_FIELD] = msisdn

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

        if not errors:
            cls._cross_validate(clean, errors)
        if errors:
            raise ValidationError(
                {cls.DESC_FIELD if k == "items_desc" else k: m for k, m in errors.items()}
            )
        return cls(**clean)

    @classmethod
    def peek(cls, raw: Mapping[str, Any]) -> dict[str, str | None]:
        """Best-effort amount/recipient/payer for the audit trail of a rejected payload."""
        fields, _ = cls._canonicalise(raw)

        def text(name: str) -> str | None:
            value = fields.get(name)
            return None if value is None else str(value)

        return {
            "amount": text("amount"),
            "recipient": text(cls.RECIPIENT_FIELD),
            "payer": text(cls.PAYER_FIELD),
            "transaction_id": text("third_party_conversation_id"),
        }

    @property
    def recipient(self) -> str:
        """The party receiving the funds."""
        return getattr(self, self.RECIPIENT_FIELD)

    @property
    def payer(self) -> str:
        """The party the funds are taken from."""
        return getattr(self, self.PAYER_FIELD)

    @property
    def transaction_id(self) -> str:
        return self.third_party_conversation_id  # type: ignore[attr-defined]

    def to_request(self) -> dict[str, str]:
        """The exact JSON body sent to M-Pesa."""
        return {key: getattr(self, name) for name, key in self.REQUEST_KEYS.items()}


_CUSTOMER_ALIASES = {
    "customermsisdn": "customer_msisdn",
    "msisdn": "customer_msisdn",
    "serviceprovidercode": "service_provider_code",
    "shortcode": "service_provider_code",
}


def _customer_request_keys(desc_key: str) -> dict[str, str]:
    return {
        "amount": "input_Amount",
        "country": "input_Country",
        "currency": "input_Currency",
        "customer_msisdn": "input_CustomerMSISDN",
        "service_provider_code": "input_ServiceProviderCode",
        "third_party_conversation_id": "input_ThirdPartyConversationID",
        "transaction_reference": "input_TransactionReference",
        "items_desc": desc_key,
    }


@dataclass(frozen=True, slots=True)
class C2BSingleStagePayload(SingleStagePayload):
    """Customer pays the business: recipient is the shortcode, payer is the customer."""

    amount: str
    customer_msisdn: str
    service_provider_code: str
    transaction_reference: str
    items_desc: str
    third_party_conversation_id: str
    country: str
    currency: str

    DESC_FIELD: ClassVar[str] = "purchased_items_desc"
    DESC_REQUEST_KEY: ClassVar[str] = "input_PurchasedItemsDesc"
    EXTRA_ALIASES: ClassVar[Mapping[str, str]] = _CUSTOMER_ALIASES
    REQUEST_KEYS: ClassVar[Mapping[str, str]] = _customer_request_keys("input_PurchasedItemsDesc")
    CODE_FIELDS: ClassVar[tuple[str, ...]] = ("service_provider_code",)
    MSISDN_FIELD: ClassVar[str | None] = "customer_msisdn"
    RECIPIENT_FIELD: ClassVar[str] = "service_provider_code"
    PAYER_FIELD: ClassVar[str] = "customer_msisdn"


@dataclass(frozen=True, slots=True)
class B2CSingleStagePayload(SingleStagePayload):
    """Business pays the customer: recipient is the customer, payer is the shortcode."""

    amount: str
    customer_msisdn: str
    service_provider_code: str
    transaction_reference: str
    items_desc: str
    third_party_conversation_id: str
    country: str
    currency: str

    DESC_FIELD: ClassVar[str] = "payment_items_desc"
    DESC_REQUEST_KEY: ClassVar[str] = "input_PaymentItemsDesc"
    EXTRA_ALIASES: ClassVar[Mapping[str, str]] = _CUSTOMER_ALIASES
    REQUEST_KEYS: ClassVar[Mapping[str, str]] = _customer_request_keys("input_PaymentItemsDesc")
    CODE_FIELDS: ClassVar[tuple[str, ...]] = ("service_provider_code",)
    MSISDN_FIELD: ClassVar[str | None] = "customer_msisdn"
    RECIPIENT_FIELD: ClassVar[str] = "customer_msisdn"
    PAYER_FIELD: ClassVar[str] = "service_provider_code"


@dataclass(frozen=True, slots=True)
class B2BSingleStagePayload(SingleStagePayload):
    """Business pays another business: primary party is debited, receiver party credited."""

    amount: str
    primary_party_code: str
    receiver_party_code: str
    transaction_reference: str
    items_desc: str
    third_party_conversation_id: str
    country: str
    currency: str

    DESC_FIELD: ClassVar[str] = "purchased_items_desc"
    DESC_REQUEST_KEY: ClassVar[str] = "input_PurchasedItemsDesc"
    EXTRA_ALIASES: ClassVar[Mapping[str, str]] = {
        "primarypartycode": "primary_party_code",
        "primary": "primary_party_code",
        "receiverpartycode": "receiver_party_code",
        "receiver": "receiver_party_code",
    }
    REQUEST_KEYS: ClassVar[Mapping[str, str]] = {
        "amount": "input_Amount",
        "receiver_party_code": "input_ReceiverPartyCode",
        "country": "input_Country",
        "currency": "input_Currency",
        "primary_party_code": "input_PrimaryPartyCode",
        "third_party_conversation_id": "input_ThirdPartyConversationID",
        "transaction_reference": "input_TransactionReference",
        "items_desc": "input_PurchasedItemsDesc",
    }
    CODE_FIELDS: ClassVar[tuple[str, ...]] = ("primary_party_code", "receiver_party_code")
    RECIPIENT_FIELD: ClassVar[str] = "receiver_party_code"
    PAYER_FIELD: ClassVar[str] = "primary_party_code"

    @classmethod
    def _cross_validate(cls, clean: dict[str, str], errors: dict[str, str]) -> None:
        if clean["primary_party_code"] == clean["receiver_party_code"]:
            errors["receiver_party_code"] = "must differ from primary_party_code"
