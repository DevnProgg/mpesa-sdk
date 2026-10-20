"""B2B-specific payload rules (two shortcodes, no customer MSISDN)."""

import pytest

import MpesaSDK as mp
from MpesaSDK.errors import ValidationError
from MpesaSDK.operations import OPERATIONS
from MpesaSDK.payloads import B2BSingleStagePayload as B2B
from MpesaSDK.transactions import TransactionType as T

LES = mp.markets.LESOTHO


@pytest.fixture
def b2b():
    return {
        "amount": "1500.00",
        "primary_party_code": "ORG002",
        "receiver_party_code": "ORG001",
        "reference": "INV0042",
        "description": "Stock purchase",
        "third_party_conversation_id": "tp0001",
    }


def test_wire_format(b2b):
    assert B2B.from_mapping(b2b, LES).to_request() == {
        "input_Amount": "1500.00",
        "input_ReceiverPartyCode": "ORG001",
        "input_Country": "LES",
        "input_Currency": "LSL",
        "input_PrimaryPartyCode": "ORG002",
        "input_ThirdPartyConversationID": "tp0001",
        "input_TransactionReference": "INV0042",
        "input_PurchasedItemsDesc": "Stock purchase",
    }


def test_primary_party_pays_and_receiver_is_credited(b2b):
    payload = B2B.from_mapping(b2b, LES)
    assert payload.payer == "ORG002" and payload.recipient == "ORG001"


def test_has_no_msisdn_field_and_rejects_one(b2b):
    with pytest.raises(ValidationError, match="customer_msisdn: unknown field"):
        B2B.from_mapping({**b2b, "customer_msisdn": "26658123456"}, LES)


def test_short_aliases_and_raw_keys(b2b):
    short = {
        "amount": 10,
        "primary": "ORG002",
        "receiver": "ORG001",
        "reference": "R1",
        "description": "x",
    }
    assert B2B.from_mapping(short, LES).receiver_party_code == "ORG001"
    raw = {
        "input_Amount": "10",
        "input_PrimaryPartyCode": "000000",
        "input_ReceiverPartyCode": "000001",
        "input_TransactionReference": "T12344C",
        "input_PurchasedItemsDesc": "Shoes",
    }
    assert B2B.from_mapping(raw, LES).payer == "000000"


def test_b2c_description_key_is_not_accepted(b2b):
    del b2b["description"]
    with pytest.raises(ValidationError, match="input_PaymentItemsDesc"):
        B2B.from_mapping({**b2b, "input_PaymentItemsDesc": "x"}, LES)


def test_both_party_codes_are_required_and_validated(b2b):
    for field in ("primary_party_code", "receiver_party_code"):
        missing = {k: v for k, v in b2b.items() if k != field}
        with pytest.raises(ValidationError) as exc:
            B2B.from_mapping(missing, LES)
        assert exc.value.errors == {field: "is required"}
        with pytest.raises(ValidationError) as exc:
            B2B.from_mapping({**b2b, field: "AB"}, LES)
        assert field in exc.value.errors


def test_paying_yourself_is_rejected(b2b):
    with pytest.raises(ValidationError, match="must differ"):
        B2B.from_mapping({**b2b, "receiver_party_code": "ORG002"}, LES)


@pytest.mark.parametrize(
    "change", [{"amount": "0"}, {"reference": "x" * 21}, {"description": " "}]
)
def test_shared_rules_still_apply(b2b, change):
    with pytest.raises(ValidationError):
        B2B.from_mapping({**b2b, **change}, LES)


def test_country_must_match_market(b2b):
    with pytest.raises(ValidationError):
        B2B.from_mapping({**b2b, "country": "GHA"}, LES)


def test_idempotency_key_generated_when_absent(b2b):
    del b2b["third_party_conversation_id"]
    assert B2B.from_mapping(b2b, LES).transaction_id != B2B.from_mapping(b2b, LES).transaction_id


def test_peek_for_rejected_payload(b2b):
    assert B2B.peek({**b2b, "amount": "bad"}) == {
        "amount": "bad",
        "recipient": "ORG001",
        "payer": "ORG002",
        "transaction_id": "tp0001",
        "related_transaction_id": None,
    }


def test_operation_registry():
    for kind, callback in [(T.B2B_SINGLE_STAGE, False), (T.B2B_SINGLE_STAGE_ASYNC, True)]:
        op = OPERATIONS[kind]
        assert op.path == "b2bPayment/" and op.payload_cls is B2B
        assert op.awaits_callback is callback
    assert mp.transactions.B2B_SINGLE_STAGE is T.B2B_SINGLE_STAGE
