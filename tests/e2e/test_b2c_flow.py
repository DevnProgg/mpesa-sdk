"""End-to-end B2C single stage through the public API."""

import json
import uuid

import httpx
import pytest

import MpesaSDK as mp

from ..conftest import API_KEY, async_accepted, error, sync_ok

B2C = mp.transactions.B2C_SINGLE_STAGE
B2C_ASYNC = mp.transactions.B2C_SINGLE_STAGE_ASYNC


@pytest.fixture
def salary():
    return {
        "amount": "250.00",
        "customer_msisdn": "26658123456",
        "service_provider_code": "ORG001",
        "reference": "SAL2026",
        "description": "Salary payment",
        "third_party_conversation_id": "tp0001",
    }


async def test_happy_path_wire_format_and_audit(provider, fake, salary):
    response = await provider.initialize_transaction(type=B2C, payload=salary)
    assert response.ok and response.status is mp.TransactionStatus.SUCCEEDED

    request = fake.payment_requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://openapi.m-pesa.com/sandbox/ipg/v2/vodacomLES/b2cPayment/"
    assert fake.payment_bodies[0] == {
        "input_Amount": "250.00",
        "input_Country": "LES",
        "input_Currency": "LSL",
        "input_CustomerMSISDN": "26658123456",
        "input_ServiceProviderCode": "ORG001",
        "input_ThirdPartyConversationID": "tp0001",
        "input_TransactionReference": "SAL2026",
        "input_PaymentItemsDesc": "Salary payment",
    }
    assert fake.tokens == [API_KEY, "SESSION-1"]

    log = response.audit.log
    assert log["transaction_type"] == "b2c_single_stage"
    assert log["amount"] == "250.00" and log["currency"] == "LSL"
    assert log["recipient"] == "26658123456" and log["payer"] == "ORG001"
    assert log["request_payload"] == fake.payment_bodies[0]
    assert log["mpesa_api_response"]["output_TransactionID"] == "49XCD123F6"
    json.dumps(log)
    assert API_KEY not in json.dumps(log)


async def test_live_environment(make_provider, fake, salary):
    await make_provider(live=True).initialize_transaction(B2C, salary)
    assert str(fake.payment_requests[0].url).endswith("/openapi/ipg/v2/vodacomLES/b2cPayment/")


async def test_async_flow_is_pending_then_completed_by_callback(provider, fake, salary):
    fake.script(async_accepted())
    response = await provider.initialize_transaction(B2C_ASYNC, salary)
    assert response.ok and response.status is mp.TransactionStatus.PENDING

    stored = response.audit.log
    callback = provider.handle_callback(
        {
            "input_OriginalConversationID": "conv-1",
            "input_TransactionID": "TX77",
            "input_ResultCode": "INS-0",
            "input_ResultDesc": "Request processed successfully",
            "input_ThirdPartyConversationID": response.transaction_id,
        }
    )
    final = callback.apply_to(mp.AuditTrail.from_dict(stored))
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.recipient == "26658123456"
    assert callback.ack()["output_ResponseDesc"] == "Successfully Accepted Result"


async def test_failed_callback_marks_the_payout_failed(provider, fake, salary):
    fake.script(async_accepted())
    response = await provider.initialize_transaction(B2C_ASYNC, salary)
    callback = provider.handle_callback(
        {
            "input_OriginalConversationID": "conv-1",
            "input_ResultCode": "INS-2051",
            "input_ResultDesc": "MSISDN invalid.",
            "input_ThirdPartyConversationID": response.transaction_id,
        }
    )
    assert callback.apply_to(response.audit).status is mp.TransactionStatus.FAILED


@pytest.mark.parametrize(
    ("code", "http_status"),
    [("INS-2006", 422), ("INS-2051", 400), ("INS-31", 400), ("INS-995", 400), ("INS-6", 401)],
)
async def test_terminal_errors_are_not_retried(provider, fake, salary, code, http_status):
    fake.script(error(code, http_status))
    response = await provider.initialize_transaction(B2C, salary)
    assert not response.ok and response.status is mp.TransactionStatus.FAILED
    assert response.err.code == code and response.audit.retry_count == 0
    assert len(fake.payment_requests) == 1


async def test_insufficient_float_message_reaches_the_caller(provider, fake, salary):
    fake.script(error("INS-2006", 422, "Insufficient balance"))
    response = await provider.initialize_transaction(B2C, salary)
    with pytest.raises(mp.TransactionError, match="Insufficient balance"):
        response.raise_for_error()


async def test_transient_failures_retry_with_one_idempotency_key(provider, fake, salary):
    """The key property for payouts: retries must never create a second disbursement."""
    fake.script(error("INS-1"), httpx.ConnectError("down"), sync_ok())
    response = await provider.initialize_transaction(B2C, salary)
    assert response.ok and response.audit.retry_count == 2
    assert {b["input_ThirdPartyConversationID"] for b in fake.payment_bodies} == {"tp0001"}
    assert [a.disposition for a in response.audit.attempts] == ["retry", "retry", "success"]


async def test_timeouts_end_as_unknown_never_as_a_blind_failure(provider, fake, salary):
    fake.script(*[httpx.ReadTimeout("slow")] * 4)
    response = await provider.initialize_transaction(B2C, salary)
    assert response.status is mp.TransactionStatus.UNKNOWN
    assert response.err.needs_reconciliation and len(fake.payment_requests) == 4


async def test_duplicate_after_a_timeout_is_unknown(provider, fake, salary):
    fake.script(httpx.ReadTimeout("slow"), error("INS-10", 409))
    response = await provider.initialize_transaction(B2C, salary)
    assert response.status is mp.TransactionStatus.UNKNOWN and response.err.needs_reconciliation


async def test_validation_failure_is_audited_with_customer_as_recipient(provider, fake, salary):
    response = await provider.initialize_transaction(B2C, {**salary, "amount": "-1"})
    assert not response.ok and response.err.code == "VALIDATION_ERROR"
    assert response.audit.recipient == "26658123456" and response.audit.payer == "ORG001"
    assert fake.payment_requests == [] and fake.session_requests == []


async def test_c2b_style_field_name_is_rejected_for_b2c(provider, fake, salary):
    bad = {k: v for k, v in salary.items() if k != "description"}
    bad["input_PurchasedItemsDesc"] = "Salary payment"
    response = await provider.initialize_transaction(B2C, bad)
    assert not response.ok and "input_PurchasedItemsDesc" in response.err.message
    assert fake.payment_requests == []


async def test_b2c_and_c2b_can_be_mixed_in_one_batch(provider, fake, salary):
    c2b = {
        "amount": "5",
        "customer_msisdn": "26658123456",
        "service_provider_code": "ORG001",
        "reference": "T1",
        "description": "Shoes",
        "third_party_conversation_id": "tp0002",
    }
    responses = await provider.initialize_many(
        [(B2C, salary), (mp.transactions.C2B_SINGLE_STAGE, c2b)]
    )
    assert all(r.ok for r in responses)
    paths = sorted(r.url.path.rsplit("/", 2)[-2] for r in fake.payment_requests)
    assert paths == ["b2cPayment", "singleStage"]


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


async def test_queue_mode_processes_b2c_with_retries(make_provider, fake, salary):
    pytest.importorskip("celery")
    queued = make_provider(**EAGER)
    salary = {**salary, "third_party_conversation_id": uuid.uuid4().hex}
    fake.script(error("INS-1"), sync_ok())

    response = await queued.initialize_transaction(B2C, salary)
    assert response.status is mp.TransactionStatus.QUEUED

    final = await queued.wait_for(response.task_id, timeout=5)
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.audit.retry_count == 1
    assert final.audit.recipient == "26658123456"
    assert "b2cPayment" in str(fake.payment_requests[0].url)


async def test_queue_mode_b2c_async_ends_pending(make_provider, fake, salary):
    pytest.importorskip("celery")
    queued = make_provider(**EAGER)
    salary = {**salary, "third_party_conversation_id": uuid.uuid4().hex}
    fake.script(async_accepted())
    response = await queued.initialize_transaction(B2C_ASYNC, salary)
    assert (
        await queued.wait_for(response.task_id, timeout=5)
    ).status is mp.TransactionStatus.PENDING
