"""Retry/terminal-state behaviour of the engine, one scripted M-Pesa reply at a time."""

import httpx
import pytest

import MpesaSDK as mp
from MpesaSDK.audit import AuditTrail
from MpesaSDK.engine import TransactionEngine
from MpesaSDK.http import MpesaHttpClient
from MpesaSDK.operations import OPERATIONS
from MpesaSDK.payloads import C2BSingleStagePayload
from MpesaSDK.retry import RetryPolicy
from MpesaSDK.session import SessionManager
from MpesaSDK.settings import parse_config
from MpesaSDK.transactions import TransactionStatus as S
from MpesaSDK.transactions import TransactionType

from ..conftest import Reply, async_accepted, error, sync_ok


@pytest.fixture
def engine(settings, fake):
    cfg = parse_config(
        {**settings, "retry-strategy": {"retry-count": 2, "back-off": 5, "jitter": 0}}
    )
    client = MpesaHttpClient(cfg, httpx.MockTransport(fake))
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)

    eng = TransactionEngine(client, SessionManager(cfg, client), RetryPolicy(cfg.retry), sleep)
    eng.slept = slept
    return eng


@pytest.fixture
def new_audit(payload):
    def make(kind=TransactionType.C2B_SINGLE_STAGE):
        p = C2BSingleStagePayload.from_mapping(payload, mp.markets.LESOTHO)
        return OPERATIONS[kind], AuditTrail(
            p.transaction_id,
            kind.value,
            p.amount,
            p.currency,
            p.recipient,
            p.payer,
            "2026-01-01T00:00:00+00:00",
            S.QUEUED,
            request_payload=p.to_request(),
        )

    return make


async def test_success_first_try(engine, fake, new_audit):
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.SUCCEEDED and result.failure is None
    assert audit.retry_count == 0 and len(audit.attempts) == 1
    assert audit.mpesa_transaction_id == "49XCD123F6" and audit.conversation_id == "conv-1"
    assert audit.completed_at and audit.mpesa_api_response["output_ResponseCode"] == "INS-0"


async def test_async_flow_is_pending_until_callback(engine, fake, new_audit):
    fake.script(async_accepted())
    op, audit = new_audit(TransactionType.C2B_SINGLE_STAGE_ASYNC)
    result = await engine.run(op, audit)
    assert result.audit.status is S.PENDING and result.failure is None
    assert audit.completed_at is None and audit.conversation_id == "conv-1"


@pytest.mark.parametrize(
    "code,status",
    [
        ("INS-2006", 422),
        ("INS-2051", 400),
        ("INS-6", 401),
        ("INS-15", 400),
        ("INS-997", 400),
    ],
)
async def test_terminal_errors_are_never_retried(engine, fake, new_audit, code, status):
    fake.script(error(code, status))
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.FAILED
    assert result.failure.code == code and not result.failure.needs_reconciliation
    assert audit.retry_count == 0 and len(fake.payment_requests) == 1
    assert engine.slept == []


async def test_transient_error_is_retried_with_backoff_then_succeeds(engine, fake, new_audit):
    fake.script(error("INS-1"), Reply(503), sync_ok())
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.SUCCEEDED
    assert audit.retry_count == 2 and len(audit.attempts) == 3
    assert engine.slept == [5.0, 10.0]
    assert [a.disposition for a in audit.attempts] == ["retry", "retry", "success"]


async def test_same_idempotency_key_on_every_attempt(engine, fake, new_audit):
    fake.script(error("INS-1"), error("INS-1"), sync_ok())
    op, audit = new_audit()
    await engine.run(op, audit)
    keys = {b["input_ThirdPartyConversationID"] for b in fake.payment_bodies}
    assert keys == {"tp0001"} and len(fake.payment_bodies) == 3


async def test_exhausted_retries_after_ambiguous_failure_is_unknown(engine, fake, new_audit):
    fake.script(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.UNKNOWN
    assert result.failure.needs_reconciliation and "unknown" in result.failure.message
    assert audit.retry_count == 2 and len(audit.attempts) == 3
    assert all(a.ambiguous for a in audit.attempts)


async def test_exhausted_retries_with_unambiguous_failure_is_failed(engine, fake, new_audit):
    fake.script(Reply(429), Reply(429), Reply(429))
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.FAILED
    assert result.failure.retryable and not result.failure.needs_reconciliation


async def test_duplicate_on_first_attempt_is_a_plain_failure(engine, fake, new_audit):
    fake.script(error("INS-10", 409))
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.FAILED and result.failure.code == "INS-10"
    assert not result.failure.needs_reconciliation and len(fake.payment_requests) == 1


async def test_duplicate_after_a_timeout_means_unknown_not_failed(engine, fake, new_audit):
    fake.script(httpx.ReadTimeout("slow"), error("INS-10", 409))
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.UNKNOWN and result.failure.needs_reconciliation
    assert len(fake.payment_requests) == 2  # and we stopped there


async def test_session_failure_is_retried_and_never_sends_a_payment(engine, fake, new_audit):
    fake.session_script.append(error("INS-989"))
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.SUCCEEDED
    assert audit.attempts[0].response_code == "SESSION_ERROR"
    assert not audit.attempts[0].ambiguous and len(fake.payment_requests) == 1


async def test_gateway_auth_failure_refreshes_session(engine, fake, new_audit):
    fake.script(Reply(401), sync_ok())
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.SUCCEEDED
    assert fake.sessions_issued == 2  # a new session was fetched for the retry
    assert fake.tokens[-1] == "SESSION-2"


async def test_session_limit_forces_new_session_for_next_transaction(engine, fake, new_audit):
    fake.script(error("INS-994"))
    op, audit = new_audit()
    result = await engine.run(op, audit)
    assert result.audit.status is S.FAILED
    op, audit2 = new_audit()
    await engine.run(op, audit2)
    assert fake.sessions_issued == 2


async def test_step_returns_retry_delay_instead_of_sleeping(engine, fake, new_audit):
    fake.script(error("INS-1"))
    op, audit = new_audit()
    step = await engine.step(op, audit)
    assert step.retry_delay == 5.0 and step.audit.status is S.RETRYING
    assert engine.slept == []


async def test_zero_retries_configured(settings, fake, new_audit):
    cfg = parse_config({**settings, "retry-strategy": {"retry-count": 0}})
    client = MpesaHttpClient(cfg, httpx.MockTransport(fake))
    eng = TransactionEngine(client, SessionManager(cfg, client), RetryPolicy(cfg.retry))
    fake.script(error("INS-1"))
    op, audit = new_audit()
    result = await eng.run(op, audit)
    assert result.audit.status is S.UNKNOWN and len(fake.payment_requests) == 1
