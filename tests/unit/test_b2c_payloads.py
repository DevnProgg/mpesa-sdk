"""B2C-specific payload rules. Shared field validation is covered in test_payloads.py."""

import pytest

import MpesaSDK as mp
from MpesaSDK.codes import KNOWN_CODES, Disposition, classify
from MpesaSDK.errors import ValidationError
from MpesaSDK.http import ApiResponse
from MpesaSDK.operations import OPERATIONS
from MpesaSDK.payloads import B2CSingleStagePayload as B2C
from MpesaSDK.payloads import C2BSingleStagePayload as C2B
from MpesaSDK.transactions import TransactionType as T

LES = mp.markets.LESOTHO


@pytest.fixture
def b2c():
    return {
        "amount": "250.00",
        "customer_msisdn": "26658123456",
        "service_provider_code": "ORG001",
        "reference": "SAL2026",
        "description": "Salary payment",
        "third_party_conversation_id": "tp0001",
    }


def test_wire_format_uses_payment_items_desc(b2c):
    assert B2C.from_mapping(b2c, LES).to_request() == {
        "input_Amount": "250.00",
        "input_Country": "LES",
        "input_Currency": "LSL",
        "input_CustomerMSISDN": "26658123456",
        "input_ServiceProviderCode": "ORG001",
        "input_ThirdPartyConversationID": "tp0001",
        "input_TransactionReference": "SAL2026",
        "input_PaymentItemsDesc": "Salary payment",
    }


def test_funds_flow_is_business_to_customer(b2c):
    payload = B2C.from_mapping(b2c, LES)
    assert payload.recipient == "26658123456" and payload.payer == "ORG001"


def test_c2b_direction_is_the_opposite(b2c):
    payload = C2B.from_mapping(b2c, LES)
    assert payload.recipient == "ORG001" and payload.payer == "26658123456"


def test_raw_openapi_keys_work_and_the_wrong_description_key_does_not(b2c):
    raw = {
        "input_Amount": "10",
        "input_CustomerMSISDN": "26658123456",
        "input_ServiceProviderCode": "000000",
        "input_TransactionReference": "T1234C",
        "input_PaymentItemsDesc": "Salary payment",
    }
    assert B2C.from_mapping(raw, LES).items_desc == "Salary payment"
    with pytest.raises(ValidationError, match="input_PaymentItemsDesc"):
        C2B.from_mapping(raw, LES)
    wrong = {**raw, "input_PurchasedItemsDesc": raw.pop("input_PaymentItemsDesc")}
    with pytest.raises(ValidationError, match="input_PurchasedItemsDesc"):
        B2C.from_mapping(wrong, LES)


def test_errors_name_the_b2c_description_field(b2c):
    del b2c["description"]
    with pytest.raises(ValidationError) as exc:
        B2C.from_mapping(b2c, LES)
    assert exc.value.errors == {"payment_items_desc": "is required"}
    with pytest.raises(ValidationError) as exc:
        B2C.from_mapping({**b2c, "description": "x" * 257}, LES)
    assert "payment_items_desc" in exc.value.errors


@pytest.mark.parametrize(
    "change",
    [{"amount": "0"}, {"customer_msisdn": "+26658123456"}, {"reference": "x" * 21}],
)
def test_shared_rules_still_apply(b2c, change):
    with pytest.raises(ValidationError):
        B2C.from_mapping({**b2c, **change}, LES)


def test_idempotency_key_generated_when_absent(b2c):
    del b2c["third_party_conversation_id"]
    assert B2C.from_mapping(b2c, LES).transaction_id != B2C.from_mapping(b2c, LES).transaction_id


def test_peek_assigns_customer_as_recipient(b2c):
    assert B2C.peek(b2c) == {
        "amount": "250.00",
        "recipient": "26658123456",
        "payer": "ORG001",
        "transaction_id": "tp0001",
    }


def test_operation_registry():
    for kind, callback in [(T.B2C_SINGLE_STAGE, False), (T.B2C_SINGLE_STAGE_ASYNC, True)]:
        op = OPERATIONS[kind]
        assert op.path == "b2cPayment/" and op.payload_cls is B2C
        assert op.awaits_callback is callback
    assert mp.transactions.B2C_SINGLE_STAGE is T.B2C_SINGLE_STAGE


def test_b2c_response_code_31_is_documented_and_terminal():
    assert KNOWN_CODES["INS-31"] == "Invalid Payment Items Description Used"
    verdict = classify(ApiResponse(400, {"output_ResponseCode": "INS-31"}))
    assert verdict.disposition is Disposition.TERMINAL
