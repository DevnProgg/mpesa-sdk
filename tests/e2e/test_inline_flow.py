"""End-to-end through the public API (mp.config -> initialize_transaction), inline mode."""

import asyncio
import json

import httpx
import pytest

import MpesaSDK as mp

from ..conftest import API_KEY, async_accepted, error, sync_ok

C2B = mp.transactions.C2B_SINGLE_STAGE
C2B_ASYNC = mp.transactions.C2B_SINGLE_STAGE_ASYNC


async def test_happy_path_and_audit_trail(provider, fake, payload):
    response = await provider.initialize_transaction(type=C2B, payload=payload)
    assert (
        response.ok and response.status is mp.TransactionStatus.SUCCEEDED and response.err is None
    )

    log = response.audit.log
    assert log["amount"] == "10.50"
    assert log["recipient"] == "ORG001" and log["payer"] == "26658123456"
    assert log["status"] == "succeeded" and log["retry_count"] == 0
    assert log["timestamp"].endswith("+00:00")
    assert log["mpesa_api_response"]["output_TransactionID"] == "49XCD123F6"
    assert log["request_payload"] == fake.payment_bodies[0]
    json.dumps(log)  # storable as-is


async def test_request_matches_the_documented_wire_format(provider, fake, payload):
    await provider.initialize_transaction(C2B, payload)
    request = fake.payment_requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://openapi.m-pesa.com/sandbox/ipg/v2/vodacomLES/c2bPayment/singleStage/"
    )
    assert request.headers["Origin"] == "*"
    assert request.headers["Content-Type"] == "application/json"
    assert fake.payment_bodies[0] == {
        "input_Amount": "10.50",
        "input_Country": "LES",
        "input_Currency": "LSL",
        "input_CustomerMSISDN": "26658123456",
        "input_ServiceProviderCode": "ORG001",
        "input_ThirdPartyConversationID": "tp0001",
        "input_TransactionReference": "T12344C",
        "input_PurchasedItemsDesc": "Handbag, Black",
    }
    # session call used the encrypted API key; the payment used the encrypted session key
    assert fake.tokens == [API_KEY, "SESSION-1"]
    assert fake.session_requests[0].method == "GET"


async def test_live_flag_switches_environment(make_provider, fake, payload):
    await make_provider(live=True).initialize_transaction(C2B, payload)
    assert "/openapi/ipg/v2/vodacomLES/" in str(fake.payment_requests[0].url)


async def test_credentials_never_reach_the_audit_log(provider, fake, payload):
    response = await provider.initialize_transaction(C2B, payload)
    blob = json.dumps(response.audit.log) + repr(response)
    assert API_KEY not in blob and "SESSION-1" not in blob


async def test_async_flow_returns_pending_then_callback_completes(provider, fake, payload):
    fake.script(async_accepted())
    response = await provider.initialize_transaction(C2B_ASYNC, payload)
    assert response.ok and response.status is mp.TransactionStatus.PENDING
    assert not response.is_final

    stored = response.audit.log  # what the developer saved to their DB
    callback = provider.handle_callback(
        {
            "input_OriginalConversationID": "conv-1",
            "input_TransactionID": "TX9",
            "input_ResultCode": "INS-0",
            "input_ResultDesc": "Request processed successfully",
            "input_ThirdPartyConversationID": response.transaction_id,
        }
    )
    final = callback.apply_to(mp.AuditTrail.from_dict(stored))
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.mpesa_transaction_id == "TX9"


async def test_terminal_failure_response_has_error_and_full_audit(provider, fake, payload):
    fake.script(error("INS-2006", 422, "Insufficient balance"))
    response = await provider.initialize_transaction(C2B, payload)
    assert not response.ok and response.status is mp.TransactionStatus.FAILED
    assert response.err.code == "INS-2006" and response.err.message == "Insufficient balance"
    assert response.audit.log["mpesa_api_response"]["output_ResponseCode"] == "INS-2006"
    assert response.audit.retry_count == 0
    with pytest.raises(mp.TransactionError, match="Insufficient balance"):
        raise mp.TransactionError(response.err.message)


async def test_transient_failures_are_retried_and_counted(provider, fake, payload):
    fake.script(error("INS-1"), httpx.ConnectError("down"), sync_ok())
    response = await provider.initialize_transaction(C2B, payload)
    assert response.ok and response.audit.retry_count == 2
    assert (
        len(response.audit.attempts) == 3 and response.audit.attempts[0].response_code == "INS-1"
    )


async def test_invalid_payload_returns_audited_failure_without_any_http_call(
    provider, fake, payload
):
    response = await provider.initialize_transaction(C2B, {**payload, "amount": "-3"})
    assert not response.ok and response.err.code == "VALIDATION_ERROR"
    assert "amount" in response.err.message
    assert response.audit.amount == "-3" and response.audit.recipient == "ORG001"
    assert response.audit.status is mp.TransactionStatus.FAILED
    assert fake.payment_requests == [] and fake.session_requests == []


async def test_unknown_transaction_type_is_a_programmer_error(provider, payload):
    with pytest.raises(ValueError):
        await provider.initialize_transaction("teleport", payload)


async def test_string_type_and_camel_case_alias(provider, payload):
    response = await provider.initializeTransaction(type="c2b_single_stage", payload=payload)
    assert response.ok


async def test_audit_handler_is_called_for_good_and_bad_outcomes(make_provider, fake, payload):
    seen = []

    async def handler(response):
        seen.append(response.status)

    provider = make_provider(**{"audit-handler": handler})
    fake.script(sync_ok(), error("INS-2006", 422))
    await provider.initialize_transaction(C2B, payload)
    await provider.initialize_transaction(C2B, {**payload, "third_party_conversation_id": "tp2"})
    assert seen == [mp.TransactionStatus.SUCCEEDED, mp.TransactionStatus.FAILED]


async def test_failing_audit_handler_never_changes_the_outcome(make_provider, payload):
    def boom(_):
        raise RuntimeError("db down")

    response = await make_provider(**{"audit-handler": boom}).initialize_transaction(C2B, payload)
    assert response.ok


async def test_initialize_many_respects_max_workers(make_provider, fake, payload):
    in_flight = peak = 0
    base = fake.__call__

    class Tracking(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal in_flight, peak
            if request.url.path.endswith("singleStage/"):
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1
            return await httpx.MockTransport(base).handle_async_request(request)

    cfg = make_provider(**{"max-workers": 3}).config
    provider = mp.MpesaProvider(cfg, transport=Tracking())
    requests = [(C2B, {**payload, "third_party_conversation_id": f"tp{i}"}) for i in range(12)]
    responses = await provider.initialize_many(requests)
    assert all(r.ok for r in responses) and len(responses) == 12
    assert 1 < peak <= 3
    assert len({b["input_ThirdPartyConversationID"] for b in fake.payment_bodies}) == 12


async def test_bad_public_key_fails_at_construction(settings):
    with pytest.raises(mp.CryptoError):
        mp.config({**settings, "public-key": "not-a-key"})


def test_queue_features_need_concurrency(provider):
    with pytest.raises(mp.ConfigError):
        provider.celery_app


async def test_non_json_gateway_error_is_retried_then_reported(make_provider, fake, payload):
    from MpesaSDK.http import ApiResponse  # noqa: F401  (documented behaviour below)

    class HtmlGateway(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request):
            if request.url.path.endswith("/getSession/"):
                return await httpx.MockTransport(fake).handle_async_request(request)
            HtmlGateway.calls += 1
            return httpx.Response(502, text="<html>Bad Gateway</html>")

    provider = mp.MpesaProvider(make_provider().config, transport=HtmlGateway())
    response = await provider.initialize_transaction(C2B, payload)
    assert response.status is mp.TransactionStatus.UNKNOWN  # 5xx may have reached M-Pesa
    assert HtmlGateway.calls == 4  # 1 attempt + 3 retries
    assert response.audit.mpesa_api_response == {"raw": "<html>Bad Gateway</html>"}


async def test_queue_only_methods_require_concurrency(provider):
    with pytest.raises(mp.ConfigError):
        await provider.get_result("x")
