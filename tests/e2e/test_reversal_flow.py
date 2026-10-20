"""End-to-end reversals through the public API."""

import json
import uuid

import httpx
import pytest

import MpesaSDK as mp

from ..conftest import API_KEY, async_accepted, error, sync_ok

REVERSAL = mp.transactions.REVERSAL
REVERSAL_ASYNC = mp.transactions.REVERSAL_ASYNC


@pytest.fixture
def reversal():
    return {
        "transaction_id": "49XCD123F6",
        "service_provider_code": "ORG001",
        "amount": "25",
        "third_party_conversation_id": "rev0001",
    }


async def test_happy_path_uses_put_and_matches_the_documented_wire_format(
    provider, fake, reversal
):
    response = await provider.initialize_transaction(type=REVERSAL, payload=reversal)
    assert response.ok and response.status is mp.TransactionStatus.SUCCEEDED

    request = fake.payment_requests[0]
    assert request.method == "PUT"
    assert str(request.url) == "https://openapi.m-pesa.com/sandbox/ipg/v2/vodacomLES/reversal/"
    assert fake.payment_bodies[0] == {
        "input_ReversalAmount": "25",
        "input_Country": "LES",
        "input_TransactionID": "49XCD123F6",
        "input_ServiceProviderCode": "ORG001",
        "input_ThirdPartyConversationID": "rev0001",
    }
    assert fake.tokens == [API_KEY, "SESSION-1"]


async def test_audit_trail_links_back_to_the_reversed_transaction(provider, fake, reversal):
    log = (await provider.initialize_transaction(REVERSAL, reversal)).audit.log
    assert log["transaction_type"] == "reversal" and log["transaction_id"] == "rev0001"
    assert log["related_transaction_id"] == "49XCD123F6"
    assert log["amount"] == "25" and log["currency"] == "LSL"
    assert log["recipient"] == "ORG001" and log["payer"] is None
    assert log["request_payload"] == fake.payment_bodies[0]
    assert log["mpesa_api_response"]["output_ResponseCode"] == "INS-0"
    json.dumps(log)
    assert API_KEY not in json.dumps(log)


async def test_full_reversal_sends_no_amount_and_audits_none(provider, fake, reversal):
    del reversal["amount"]
    response = await provider.initialize_transaction(REVERSAL, reversal)
    assert response.ok and "input_ReversalAmount" not in fake.payment_bodies[0]
    assert response.audit.amount is None


async def test_reverse_a_payment_using_its_audit_log(provider, fake, payload):
    """The real workflow: pay, keep the audit log, later reverse by the M-Pesa transaction id."""
    paid = await provider.initialize_transaction(mp.transactions.C2B_SINGLE_STAGE, payload)
    assert paid.ok
    stored = paid.audit.log  # what the developer saved
    mpesa_id = stored["mpesa_transaction_id"]
    assert mpesa_id == "49XCD123F6"

    undone = await provider.initialize_transaction(
        REVERSAL,
        {"transaction_id": mpesa_id, "service_provider_code": payload["service_provider_code"]},
    )
    assert undone.ok and undone.audit.related_transaction_id == mpesa_id
    assert undone.transaction_id != paid.transaction_id  # its own idempotency key
    assert fake.payment_requests[0].method == "POST" and fake.payment_requests[1].method == "PUT"
    assert fake.payment_bodies[1]["input_TransactionID"] == mpesa_id


async def test_live_environment(make_provider, fake, reversal):
    await make_provider(live=True).initialize_transaction(REVERSAL, reversal)
    assert str(fake.payment_requests[0].url).endswith("/openapi/ipg/v2/vodacomLES/reversal/")


async def test_async_flow_is_pending_then_completed_by_callback(provider, fake, reversal):
    fake.script(async_accepted())
    response = await provider.initialize_transaction(REVERSAL_ASYNC, reversal)
    assert response.ok and response.status is mp.TransactionStatus.PENDING
    assert fake.payment_requests[0].method == "PUT"

    callback = provider.handle_callback(
        {
            "input_OriginalConversationID": "conv-1",
            "input_TransactionID": "REV123",
            "input_ResultCode": "INS-0",
            "input_ResultDesc": "Request processed successfully",
            "input_ThirdPartyConversationID": response.transaction_id,
        }
    )
    final = callback.apply_to(mp.AuditTrail.from_dict(response.audit.log))
    assert final.status is mp.TransactionStatus.SUCCEEDED
    assert final.mpesa_transaction_id == "REV123"
    assert final.related_transaction_id == "49XCD123F6"


@pytest.mark.parametrize(
    ("code", "http_status"),
    [("INS-18", 400), ("INS-35", 400), ("INS-2006", 422), ("INS-996", 400), ("INS-6", 401)],
)
async def test_terminal_errors_are_not_retried(provider, fake, reversal, code, http_status):
    fake.script(error(code, http_status))
    response = await provider.initialize_transaction(REVERSAL, reversal)
    assert not response.ok and response.status is mp.TransactionStatus.FAILED
    assert response.err.code == code and response.audit.retry_count == 0
    assert len(fake.payment_requests) == 1


async def test_not_your_transaction_message_reaches_the_caller(provider, fake, reversal):
    fake.script(error("INS-35", 400, "This transaction do not belong to you"))
    response = await provider.initialize_transaction(REVERSAL, reversal)
    with pytest.raises(mp.TransactionError, match="do not belong to you"):
        response.raise_for_error()


async def test_transient_failures_retry_with_one_idempotency_key_and_put(provider, fake, reversal):
    fake.script(error("INS-1"), httpx.ConnectError("down"), sync_ok())
    response = await provider.initialize_transaction(REVERSAL, reversal)
    assert response.ok and response.audit.retry_count == 2
    assert {b["input_ThirdPartyConversationID"] for b in fake.payment_bodies} == {"rev0001"}
    assert {r.method for r in fake.payment_requests} == {"PUT"}


async def test_unresolved_timeouts_end_as_unknown_never_a_blind_second_reversal(
    provider, fake, reversal
):
    fake.script(*[httpx.ReadTimeout("slow")] * 4)
    response = await provider.initialize_transaction(REVERSAL, reversal)
    assert response.status is mp.TransactionStatus.UNKNOWN and response.err.needs_reconciliation
    assert response.audit.related_transaction_id == "49XCD123F6"


async def test_duplicate_reversal_id_is_a_plain_failure(provider, fake, reversal):
    fake.script(error("INS-10", 409))
    response = await provider.initialize_transaction(REVERSAL, reversal)
    assert response.status is mp.TransactionStatus.FAILED and response.err.code == "INS-10"


async def test_validation_failure_is_audited_and_sends_nothing(provider, fake, reversal):
    response = await provider.initialize_transaction(
        REVERSAL, {**reversal, "transaction_id": "not valid!"}
    )
    assert not response.ok and response.err.code == "VALIDATION_ERROR"
    assert response.audit.related_transaction_id == "not valid!"
    assert response.audit.recipient == "ORG001" and response.audit.payer is None
    assert fake.payment_requests == [] and fake.session_requests == []


async def test_currency_is_not_accepted_on_a_reversal(provider, fake, reversal):
    response = await provider.initialize_transaction(REVERSAL, {**reversal, "currency": "LSL"})
    assert not response.ok and "currency" in response.err.message
    assert fake.payment_requests == []


async def test_mixed_batch_of_payments_and_reversals(provider, fake, reversal, payload):
    responses = await provider.initialize_many(
        [(REVERSAL, reversal), (mp.transactions.C2B_SINGLE_STAGE, payload)]
    )
    assert all(r.ok for r in responses)
    assert sorted(r.method for r in fake.payment_requests) == ["POST", "PUT"]


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


async def test_queue_mode_processes_reversals_with_retries(make_provider, fake, reversal):
    pytest.importorskip("celery")
    queued = make_provider(**EAGER)
    reversal = {**reversal, "third_party_conversation_id": uuid.uuid4().hex}
    fake.script(error("INS-1"), sync_ok())

    response = await queued.initialize_transaction(REVERSAL, reversal)
    assert response.status is mp.TransactionStatus.QUEUED

    final = await queued.wait_for(response.task_id, timeout=5)
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.audit.retry_count == 1
    assert final.audit.related_transaction_id == "49XCD123F6"
    assert {r.method for r in fake.payment_requests} == {"PUT"}
