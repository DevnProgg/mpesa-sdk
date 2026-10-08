"""Queue mode. Eager tests run anywhere; the integration test uses a real Redis + worker."""

import os
import uuid

import httpx
import pytest

pytest.importorskip("celery")
import MpesaSDK as mp

from ..conftest import API_KEY, async_accepted, error, sync_ok

C2B = mp.transactions.C2B_SINGLE_STAGE
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


@pytest.fixture
def payload(payload):
    """Unique idempotency key per test: Celery task ids (== transaction ids) must be unique."""
    return {**payload, "third_party_conversation_id": uuid.uuid4().hex}


@pytest.fixture
def queued(make_provider):
    return make_provider(**EAGER)


async def test_returns_queued_immediately_then_result_is_fetchable(queued, fake, payload):
    response = await queued.initialize_transaction(C2B, payload)
    assert response.ok and response.status is mp.TransactionStatus.QUEUED
    tx = payload["third_party_conversation_id"]
    assert response.task_id == response.transaction_id == tx

    final = await queued.wait_for(response.task_id, timeout=5)
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.ok
    assert final.audit.log["mpesa_api_response"]["output_ResponseCode"] == "INS-0"
    assert final.task_id == tx


async def test_retries_via_celery_keep_the_audit_trail(queued, fake, payload):
    fake.script(error("INS-1"), httpx.ReadTimeout("slow"), sync_ok())
    response = await queued.initialize_transaction(C2B, payload)
    final = await queued.wait_for(response.task_id, timeout=5)
    assert final.ok and final.audit.retry_count == 2 and len(final.audit.attempts) == 3
    assert {b["input_ThirdPartyConversationID"] for b in fake.payment_bodies} == {
        payload["third_party_conversation_id"]
    }


async def test_terminal_error_is_not_retried_by_the_worker(queued, fake, payload):
    fake.script(error("INS-2006", 422))
    response = await queued.initialize_transaction(C2B, payload)
    final = await queued.wait_for(response.task_id, timeout=5)
    assert not final.ok and final.err.code == "INS-2006" and final.audit.retry_count == 0
    assert len(fake.payment_requests) == 1


async def test_exhausted_timeouts_end_as_unknown(queued, fake, payload):
    fake.script(*[httpx.ReadTimeout("slow")] * 4)
    final = await queued.wait_for(
        (await queued.initialize_transaction(C2B, payload)).task_id, timeout=5
    )
    assert final.status is mp.TransactionStatus.UNKNOWN and final.err.needs_reconciliation


async def test_async_type_ends_pending_in_the_worker(queued, fake, payload):
    fake.script(async_accepted())
    r = await queued.initialize_transaction(mp.transactions.C2B_SINGLE_STAGE_ASYNC, payload)
    assert (await queued.wait_for(r.task_id, timeout=5)).status is mp.TransactionStatus.PENDING


async def test_audit_handler_runs_in_the_worker(make_provider, fake, payload):
    seen = []
    provider = make_provider(**EAGER, **{"audit-handler": lambda r: seen.append(r.audit.log)})
    r = await provider.initialize_transaction(C2B, payload)
    await provider.wait_for(r.task_id, timeout=5)
    assert len(seen) == 1 and seen[0]["status"] == "succeeded"


async def test_validation_errors_never_touch_the_queue(queued, fake, payload):
    response = await queued.initialize_transaction(C2B, {**payload, "amount": "x"})
    assert response.status is mp.TransactionStatus.FAILED and response.task_id is None


async def test_unexpected_worker_bug_still_yields_an_audit(queued, fake, payload, monkeypatch):
    async def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(queued._engine, "step", explode)
    r = await queued.initialize_transaction(C2B, payload)
    final = await queued.wait_for(r.task_id, timeout=5)
    assert final.status is mp.TransactionStatus.UNKNOWN and final.err.code == "INTERNAL_ERROR"


async def test_unreachable_broker_returns_an_audited_failure_quickly(make_provider, payload):
    import time

    provider = make_provider(**{"concurrency": True, "redis-url": "redis://127.0.0.1:1/0"})
    started = time.monotonic()
    response = await provider.initialize_transaction(C2B, payload)
    assert time.monotonic() - started < 10  # fail fast, do not hang on a dead broker
    assert not response.ok and response.err.code == "QUEUE_UNAVAILABLE" and response.err.retryable
    assert response.audit.status is mp.TransactionStatus.FAILED


async def test_get_result_is_none_while_unfinished(queued):
    assert await queued.get_result("never-submitted") is None
    with pytest.raises(TimeoutError):
        await queued.wait_for("never-submitted", timeout=0.2, poll_interval=0.05)


async def test_worker_settings_come_from_config(queued):
    conf = queued.celery_app.conf
    assert conf.worker_concurrency == 3 and conf.task_default_queue == "mpesa"
    assert conf.task_acks_late and conf.accept_content == ["json"]


async def test_credentials_are_not_in_task_messages(queued, payload):
    spec = {
        "type": "c2b_single_stage",
        "audit": (await queued.initialize_transaction(C2B, payload)).audit.to_dict(),
    }
    assert API_KEY not in str(spec)


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="set REDIS_URL to run")
async def test_real_redis_and_real_worker(make_provider, fake, payload, private_key):
    from celery.contrib.testing.worker import start_worker

    from ..conftest import FakeMpesa

    # Decoy built first: a worker must run *its own* provider's engine, never the decoy's
    # (regression for Celery's shared=True task registration).
    decoy_fake = FakeMpesa(private_key)
    make_provider(**EAGER)  # unrelated app created earlier in the process
    decoy = make_provider(**{**EAGER, "market": mp.markets.GHANA})
    decoy._engine._client._transport = httpx.MockTransport(decoy_fake)

    redis_url = os.environ["REDIS_URL"]
    provider = make_provider(
        **{
            "concurrency": True,
            "redis-url": redis_url,
            "queue": f"mpesa-test-{uuid.uuid4().hex[:6]}",
        }
    )
    fake.script(error("INS-1"), sync_ok())  # one transient failure forces a real Celery retry
    with start_worker(
        provider.celery_app, perform_ping_check=False, pool="solo", loglevel="WARNING"
    ):
        queued = await provider.initialize_transaction(C2B, payload)
        assert queued.status is mp.TransactionStatus.QUEUED
        final = await provider.wait_for(queued.task_id, timeout=30)
    assert final.status is mp.TransactionStatus.SUCCEEDED and final.audit.retry_count == 1
    assert decoy_fake.payment_requests == []


async def test_two_providers_in_one_process_stay_isolated(settings, private_key, payload):
    """Two providers (two markets) in one process each talk to their own M-Pesa platform.

    Eager mode cannot reproduce the by-name task-registry leak; the real-worker test does.
    """
    from ..conftest import FakeMpesa

    fake_a, fake_b = FakeMpesa(private_key), FakeMpesa(private_key)

    def build(fake, market):
        return mp.config(
            {**settings, **EAGER, "market": market}, transport=httpx.MockTransport(fake)
        )

    lesotho, ghana = build(fake_a, mp.markets.LESOTHO), build(fake_b, mp.markets.GHANA)
    ghana_payload = {
        **payload,
        "customer_msisdn": "233201234567",
        "third_party_conversation_id": uuid.uuid4().hex,
    }

    await lesotho.wait_for((await lesotho.initialize_transaction(C2B, payload)).task_id, timeout=5)
    await ghana.wait_for(
        (await ghana.initialize_transaction(C2B, ghana_payload)).task_id, timeout=5
    )

    assert len(fake_a.payment_requests) == 1 and len(fake_b.payment_requests) == 1
    assert "vodacomLES" in str(fake_a.payment_requests[0].url)
    assert "vodafoneGHA" in str(fake_b.payment_requests[0].url)
    assert fake_b.payment_bodies[0]["input_Currency"] == "GHS"
