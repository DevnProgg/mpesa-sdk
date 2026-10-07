import httpx
import pytest

import MpesaSDK as mp
from MpesaSDK.callbacks import parse_callback
from MpesaSDK.errors import SessionError, ValidationError
from MpesaSDK.http import MpesaHttpClient
from MpesaSDK.session import SessionManager
from MpesaSDK.settings import parse_config

from ..conftest import Reply
from .test_codes_and_audit import make_audit


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def build(settings, fake):
    def make(**overrides):
        cfg = parse_config({**settings, **overrides})
        client = MpesaHttpClient(cfg, httpx.MockTransport(fake))
        clock = Clock()
        slept: list[float] = []

        async def sleep(s):
            slept.append(s)

        return SessionManager(cfg, client, clock, sleep), clock, slept

    return make


async def test_session_is_cached(build, fake):
    sessions, *_ = build()
    assert await sessions.get() == await sessions.get() == "SESSION-1"
    assert fake.sessions_issued == 1


async def test_session_expires_after_ttl(build, fake):
    sessions, clock, _ = build(**{"session-ttl": 100})
    await sessions.get()
    clock.now = 101
    assert await sessions.get() == "SESSION-2"


async def test_invalidate(build, fake):
    sessions, *_ = build()
    first = await sessions.get()
    sessions.invalidate("some-other-session")  # stale id: ignored
    assert await sessions.get() == first
    sessions.invalidate(first)
    assert await sessions.get() == "SESSION-2"


async def test_concurrent_callers_share_one_session_call(build, fake):
    import asyncio

    sessions, *_ = build()
    results = await asyncio.gather(*(sessions.get() for _ in range(10)))
    assert set(results) == {"SESSION-1"} and fake.sessions_issued == 1


async def test_api_key_is_what_gets_encrypted_for_the_session_call(build, fake):
    from ..conftest import API_KEY

    sessions, *_ = build()
    await sessions.get()
    assert fake.tokens == [API_KEY]


async def test_warmup_delay_is_applied(build):
    sessions, _, slept = build(**{"session-warmup": 30})
    await sessions.get()
    assert slept == [30]


async def test_failures_raise_session_error(build, fake):
    sessions, *_ = build()
    fake.session_script.append(
        Reply(
            400,
            {"output_ResponseCode": "INS-989", "output_ResponseDesc": "Session Creation Failed"},
        )
    )
    with pytest.raises(SessionError, match="Session Creation Failed"):
        await sessions.get()
    fake.session_script.append(httpx.ConnectError("down"))
    with pytest.raises(SessionError, match="ConnectError"):
        await sessions.get()


GOOD_CALLBACK = {
    "input_OriginalConversationID": "conv-1",
    "input_TransactionID": "49XCD123F6",
    "input_ResultCode": "INS-0",
    "input_ResultDesc": "Request processed successfully",
    "input_ThirdPartyConversationID": "tp1",
}


def test_callback_success_updates_audit():
    result = parse_callback(GOOD_CALLBACK)
    assert result.ok
    audit = result.apply_to(make_audit(status=mp.TransactionStatus.PENDING))
    assert audit.status is mp.TransactionStatus.SUCCEEDED
    assert audit.mpesa_transaction_id == "49XCD123F6" and audit.callback == GOOD_CALLBACK
    assert audit.completed_at


def test_callback_failure_and_ack():
    result = parse_callback({**GOOD_CALLBACK, "input_ResultCode": "INS-2006"})
    assert not result.ok
    audit = result.apply_to(make_audit(status=mp.TransactionStatus.PENDING))
    assert audit.status is mp.TransactionStatus.FAILED
    assert result.ack() == {
        "output_OriginalConversationID": "conv-1",
        "output_ResponseCode": "0",
        "output_ResponseDesc": "Successfully Accepted Result",
        "output_ThirdPartyConversationID": "tp1",
    }


def test_callback_validation_and_mismatch():
    with pytest.raises(ValidationError):
        parse_callback({"input_ResultCode": "INS-0"})
    with pytest.raises(ValueError):
        parse_callback({**GOOD_CALLBACK, "input_ThirdPartyConversationID": "other"}).apply_to(
            make_audit()
        )


def test_handle_callback_accepts_raw_json():
    import json

    assert mp.MpesaProvider.handle_callback(json.dumps(GOOD_CALLBACK)).ok
