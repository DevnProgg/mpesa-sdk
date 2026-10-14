"""End-to-end B2B single stage through the public API."""

import json
import uuid

import httpx
import pytest

import MpesaSDK as mp

from ..conftest import API_KEY, async_accepted, error, sync_ok

B2B = mp.transactions.B2B_SINGLE_STAGE
B2B_ASYNC = mp.transactions.B2B_SINGLE_STAGE_ASYNC


@pytest.fixture
def invoice():
    return {
        "amount": "1500.00",
        "primary_party_code": "ORG002",
        "receiver_party_code": "ORG001",
        "reference": "INV0042",
        "description": "Stock purchase",
        "third_party_conversation_id": "tp0001",
    }


async def test_happy_path_wire_format_and_audit(provider, fake, invoice):
    response = await provider.initialize_transaction(type=B2B, payload=invoice)
    assert response.ok and response.status is mp.TransactionStatus.SUCCEEDED

    request = fake.payment_requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://openapi.m-pesa.com/sandbox/ipg/v2/vodacomLES/b2bPayment/"
    assert fake.payment_bodies[0] == {
        "input_Amount": "1500.00",
        "input_ReceiverPartyCode": "ORG001",
        "input_Country": "LES",
        "input_Currency": "LSL",
        "input_PrimaryPartyCode": "ORG002",
        "input_ThirdPartyConversationID": "tp0001",
        "input_TransactionReference": "INV0042",
        "input_PurchasedItemsDesc": "Stock purchase",
    }
    assert fake.tokens == [API_KEY, "SESSION-1"]

    log = response.audit.log
    assert log["transaction_type"] == "b2b_single_stage"
    assert log["amount"] == "1500.00" and log["currency"] == "LSL"
    assert log["payer"] == "ORG002" and log["recipient"] == "ORG001"
    assert log["request_payload"] == fake.payment_bodies[0]
    assert log["mpesa_api_response"]["output_TransactionID"] == "49XCD123F6"
    json.dumps(log)
    assert API_KEY not in json.dumps(log)


async def test_live_environment(make_provider, fake, invoice):
    await make_provider(live=True).initialize_transaction(B2B, invoice)
    assert str(fake.payment_requests[0].url).endswith("/openapi/ipg/v2/vodacomLES/b2bPayment/")


async def test_async_flow_is_pending_then_completed_by_callback(provider, fake, invoice):
    fake.script(async_accepted())
    response = await provider.initialize_transaction(B2B_ASYNC, invoice)
    assert response.ok and response.status is mp.TransactionStatus.PENDING

    callback = provider.handle_callback(
        {
            "input_OriginalConversationID": "conv-1",
            "input_TransactionID": "TX55",
            "input_ResultCode": "INS-0",
            "input_ResultDesc": "Request processed successfully",
            "input_ThirdPartyConversationID": response.transaction_id,
        }
    )
    final = callback.apply_to(mp.AuditTrail.from_dict(response.audit.log))
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.mpesa_transaction_id == "TX55"
    assert final.recipient == "ORG001" and final.payer == "ORG002"


@pytest.mark.parametrize(
    ("code", "http_status"),
    [("INS-2006", 422), ("INS-13", 400), ("INS-30", 400), ("INS-994", 400), ("INS-6", 401)],
)
async def test_terminal_errors_are_not_retried(provider, fake, invoice, code, http_status):
    fake.script(error(code, http_status))
    response = await provider.initialize_transaction(B2B, invoice)
    assert not response.ok and response.status is mp.TransactionStatus.FAILED
    assert response.err.code == code and response.audit.retry_count == 0
    assert len(fake.payment_requests) == 1


async def test_transient_failures_retry_with_one_idempotency_key(provider, fake, invoice):
    fake.script(error("INS-1"), httpx.ConnectError("down"), sync_ok())
    response = await provider.initialize_transaction(B2B, invoice)
    assert response.ok and response.audit.retry_count == 2
    assert {b["input_ThirdPartyConversationID"] for b in fake.payment_bodies} == {"tp0001"}


async def test_unresolved_timeouts_end_as_unknown(provider, fake, invoice):
    fake.script(*[httpx.ReadTimeout("slow")] * 4)
    response = await provider.initialize_transaction(B2B, invoice)
    assert response.status is mp.TransactionStatus.UNKNOWN and response.err.needs_reconciliation


async def test_validation_failure_is_audited_and_sends_nothing(provider, fake, invoice):
    response = await provider.initialize_transaction(B2B, {**invoice, "amount": "-5"})
    assert not response.ok and response.err.code == "VALIDATION_ERROR"
    assert response.audit.payer == "ORG002" and response.audit.recipient == "ORG001"
    assert fake.payment_requests == [] and fake.session_requests == []


async def test_msisdn_payload_is_rejected_before_any_network_call(provider, fake, invoice):
    wrong = {**invoice, "customer_msisdn": "26658123456"}
    response = await provider.initialize_transaction(B2B, wrong)
    assert not response.ok and "customer_msisdn" in response.err.message
    assert fake.payment_requests == []


async def test_paying_the_same_shortcode_is_rejected(provider, fake, invoice):
    response = await provider.initialize_transaction(
        B2B, {**invoice, "receiver_party_code": "ORG002"}
    )
    assert not response.ok and "must differ" in response.err.message
    assert fake.payment_requests == []


async def test_all_three_transaction_families_in_one_batch(provider, fake, invoice):
    customer = {
        "amount": "5",
        "customer_msisdn": "26658123456",
        "service_provider_code": "ORG001",
        "reference": "T1",
        "description": "Shoes",
    }
    responses = await provider.initialize_many(
        [
            (B2B, invoice),
            (mp.transactions.C2B_SINGLE_STAGE, {**customer, "third_party_conversation_id": "c1"}),
            (mp.transactions.B2C_SINGLE_STAGE, {**customer, "third_party_conversation_id": "c2"}),
        ]
    )
    assert all(r.ok for r in responses)
    paths = sorted(r.url.path.rstrip("/").rsplit("/", 1)[-1] for r in fake.payment_requests)
    assert paths == ["b2bPayment", "b2cPayment", "singleStage"]


EAGER = {
    "concurrency": True,
    "max-workers": 3,
    "celery-options": {
        "task_always_eager": True,
        "task_store_eager_result": True,
        "broker_url": "memory://",
        "result_backend": "cache+memory://",
    },
}


async def test_queue_mode_processes_b2b_with_retries(make_provider, fake, invoice):
    pytest.importorskip("celery")
    queued = make_provider(**EAGER)
    invoice = {**invoice, "third_party_conversation_id": uuid.uuid4().hex}
    fake.script(error("INS-1"), sync_ok())

    response = await queued.initialize_transaction(B2B, invoice)
    assert response.status is mp.TransactionStatus.QUEUED

    final = await queued.wait_for(response.task_id, timeout=5)
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.audit.retry_count == 1
    assert final.audit.payer == "ORG002" and final.audit.recipient == "ORG001"
    assert "b2bPayment" in str(fake.payment_requests[0].url)
