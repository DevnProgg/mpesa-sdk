"""Reversal payload rules: optional amount, original transaction id, no currency/reference."""

import pytest

import MpesaSDK as mp
from MpesaSDK.audit import AuditTrail
from MpesaSDK.codes import KNOWN_CODES, Disposition, classify
from MpesaSDK.errors import ValidationError
from MpesaSDK.http import ApiResponse
from MpesaSDK.operations import OPERATIONS
from MpesaSDK.payloads import ReversalPayload as R
from MpesaSDK.transactions import TransactionType as T

LES = mp.markets.LESOTHO


@pytest.fixture
def reversal():
    return {
        "transaction_id": "49XCD123F6",
        "service_provider_code": "ORG001",
        "amount": "25",
        "third_party_conversation_id": "rev0001",
    }


def test_partial_reversal_wire_format(reversal):
    assert R.from_mapping(reversal, LES).to_request() == {
        "input_ReversalAmount": "25",
        "input_Country": "LES",
        "input_TransactionID": "49XCD123F6",
        "input_ServiceProviderCode": "ORG001",
        "input_ThirdPartyConversationID": "rev0001",
    }


def test_full_reversal_omits_the_amount_key_entirely(reversal):
    del reversal["amount"]
    payload = R.from_mapping(reversal, LES)
    assert "input_ReversalAmount" not in payload.to_request()
    assert payload.amount is None and payload.reversal_amount is None


def test_no_currency_reference_or_description_in_the_body(reversal):
    body = R.from_mapping(reversal, LES).to_request()
    assert not {"input_Currency", "input_TransactionReference", "input_PurchasedItemsDesc"} & set(
        body
    )


@pytest.mark.parametrize("key", ["currency", "reference", "description", "customer_msisdn"])
def test_payment_only_fields_are_rejected(reversal, key):
    with pytest.raises(ValidationError, match=f"{key}: unknown field"):
        R.from_mapping({**reversal, key: "x"}, LES)


def test_aliases_and_raw_keys(reversal):
    raw = {
        "input_TransactionID": "0000000000001",
        "input_ServiceProviderCode": "000000",
        "input_ReversalAmount": "25",
        "input_ThirdPartyConversationID": "asv02e5958774f7ba228d83d0d689761",
        "input_Country": "LES",
    }
    assert R.from_mapping(raw, LES).original_transaction_id == "0000000000001"
    renamed = {k: v for k, v in reversal.items() if k not in ("transaction_id", "amount")}
    payload = R.from_mapping(
        {
            **renamed,
            "original_transaction_id": reversal["transaction_id"],
            "reversal_amount": reversal["amount"],
        },
        LES,
    )
    assert payload.original_transaction_id == "49XCD123F6" and payload.reversal_amount == "25"


def test_same_field_under_two_names_is_rejected(reversal):
    with pytest.raises(ValidationError, match="provided more than once"):
        R.from_mapping({**reversal, "original_transaction_id": "OTHER1"}, LES)


@pytest.mark.parametrize("bad", ["", "49-XCD", "x" * 21, "49 XCD", 123])
def test_original_transaction_id_rules(reversal, bad):
    with pytest.raises(ValidationError) as exc:
        R.from_mapping({**reversal, "transaction_id": bad}, LES)
    assert "original_transaction_id" in exc.value.errors


def test_transaction_id_and_shortcode_are_required(reversal):
    with pytest.raises(ValidationError) as exc:
        R.from_mapping({"amount": "5"}, LES)
    assert set(exc.value.errors) == {"original_transaction_id", "service_provider_code"}


@pytest.mark.parametrize("amount", [0, -1, "abc", True])
def test_amount_when_given_must_be_valid(reversal, amount):
    with pytest.raises(ValidationError) as exc:
        R.from_mapping({**reversal, "amount": amount}, LES)
    assert "reversal_amount" in exc.value.errors


def test_country_must_match_market(reversal):
    with pytest.raises(ValidationError):
        R.from_mapping({**reversal, "country": "GHA"}, LES)


def test_idempotency_key_generated_when_absent(reversal):
    del reversal["third_party_conversation_id"]
    assert (
        R.from_mapping(reversal, LES).transaction_id
        != R.from_mapping(reversal, LES).transaction_id
    )


def test_roles_and_related_transaction(reversal):
    payload = R.from_mapping(reversal, LES)
    assert payload.recipient == "ORG001" and payload.payer is None
    # transaction_id stays the idempotency key; the reversed one is "related"
    assert payload.transaction_id == "rev0001"
    assert payload.related_transaction_id == "49XCD123F6"


def test_peek_for_rejected_payload(reversal):
    assert R.peek({**reversal, "amount": "bad"}) == {
        "amount": "bad",
        "recipient": "ORG001",
        "payer": None,
        "transaction_id": "rev0001",
        "related_transaction_id": "49XCD123F6",
    }


def test_operation_registry_uses_put():
    for kind, callback in [(T.REVERSAL, False), (T.REVERSAL_ASYNC, True)]:
        op = OPERATIONS[kind]
        assert op.method == "PUT" and op.path == "reversal/" and op.payload_cls is R
        assert op.awaits_callback is callback
    assert all(op.method == "POST" for t, op in OPERATIONS.items() if "REVERSAL" not in t.name)
    assert mp.transactions.REVERSAL is T.REVERSAL


@pytest.mark.parametrize("code", ["INS-18", "INS-35"])
def test_reversal_response_codes_are_documented_and_terminal(code):
    assert code in KNOWN_CODES
    verdict = classify(ApiResponse(400, {"output_ResponseCode": code}))
    assert verdict.disposition is Disposition.TERMINAL


def test_audit_trail_without_the_new_field_still_loads():
    """Logs stored by earlier versions have no related_transaction_id."""
    stored = AuditTrail(
        "t1",
        "c2b_single_stage",
        "1",
        "LSL",
        "ORG1",
        "2665",
        "2026-01-01T00:00:00+00:00",
        mp.TransactionStatus.SUCCEEDED,
    ).to_dict()
    del stored["related_transaction_id"]
    assert AuditTrail.from_dict(stored).related_transaction_id is None
