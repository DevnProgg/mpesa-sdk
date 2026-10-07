import pytest

import MpesaSDK as mp
from MpesaSDK.errors import ValidationError
from MpesaSDK.payloads import C2BSingleStagePayload as P

LES = mp.markets.LESOTHO


def parse(payload, market=LES):
    return P.from_mapping(payload, market)


def test_hyphen_in_third_party_id_is_rejected(payload):
    # \w+ in the docs excludes "-"
    with pytest.raises(ValidationError) as exc:
        parse({**payload, "third_party_conversation_id": "tp-0001"})
    assert "third_party_conversation_id" in exc.value.errors


def test_raw_openapi_keys_are_accepted():
    p = parse(
        {
            "input_Amount": "10",
            "input_CustomerMSISDN": "26658123456",
            "input_ServiceProviderCode": "000000",
            "input_TransactionReference": "T1234C",
            "input_PurchasedItemsDesc": "Shoes",
            "input_ThirdPartyConversationID": "asv02e5",
            "input_Country": "LES",
            "input_Currency": "LSL",
        }
    )
    assert p.amount == "10" and p.recipient == "000000" and p.payer == "26658123456"


@pytest.fixture
def good(payload):
    return {**payload, "third_party_conversation_id": "tp0001"}


def test_valid_payload_to_request(good):
    req = parse(good).to_request()
    assert req["input_Amount"] == "10.50" and req["input_Country"] == "LES"
    assert req["input_ThirdPartyConversationID"] == "tp0001"


def test_idempotency_key_is_generated_when_absent(good):
    del good["third_party_conversation_id"]
    a, b = parse(good), parse(good)
    assert len(a.transaction_id) == 32 and a.transaction_id != b.transaction_id


@pytest.mark.parametrize("amount", [10, 10.5, "10", "0.01", " 7 ", "1e3"])
def test_valid_amounts(good, amount):
    assert parse({**good, "amount": amount}).amount


@pytest.mark.parametrize("amount", [0, -5, "abc", "", None, True, float("nan"), "Infinity"])
def test_invalid_amounts(good, amount):
    with pytest.raises(ValidationError) as exc:
        parse({**good, "amount": amount})
    assert "amount" in exc.value.errors


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_provider_code", "AB"),
        ("service_provider_code", "ORG-001"),
        ("reference", "x" * 21),
        ("reference", ""),
        ("description", "   "),
        ("description", "x" * 257),
        ("third_party_conversation_id", "x" * 41),
    ],
)
def test_field_rules(good, field, value):
    with pytest.raises(ValidationError):
        parse({**good, field: value})


def test_msisdn_rules_are_per_market(good):
    assert parse({**good, "customer_msisdn": "26658123456"}, LES)  # 11 digits
    with pytest.raises(ValidationError):
        parse({**good, "customer_msisdn": "26658123456"}, mp.markets.GHANA)  # needs 12-14
    assert parse({**good, "customer_msisdn": "233201234567"}, mp.markets.GHANA)
    with pytest.raises(ValidationError):
        parse({**good, "customer_msisdn": "+26658123456"})


def test_missing_fields_are_all_reported(good):
    with pytest.raises(ValidationError) as exc:
        parse({"amount": "5"})
    assert set(exc.value.errors) >= {
        "customer_msisdn",
        "service_provider_code",
        "transaction_reference",
        "purchased_items_desc",
    }


def test_unknown_and_duplicate_keys(good):
    with pytest.raises(ValidationError, match="unknown field"):
        parse({**good, "tip": 1})
    with pytest.raises(ValidationError, match="more than once"):
        parse({**good, "input_Amount": "5"})


def test_country_must_match_market(good):
    with pytest.raises(ValidationError):
        parse({**good, "country": "GHA"})
    assert parse({**good, "country": "LES", "currency": "LSL"})


def test_peek_is_best_effort():
    peek = P.peek({"amount": "abc", "shortcode": "ORG1", "msisdn": 5})
    assert peek == {"amount": "abc", "recipient": "ORG1", "payer": "5", "transaction_id": None}
