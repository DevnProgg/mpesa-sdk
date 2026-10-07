import json

import pytest

from MpesaSDK.audit import AuditTrail
from MpesaSDK.codes import KNOWN_CODES, Disposition, classify
from MpesaSDK.http import ApiResponse
from MpesaSDK.response import TransactionFailure, TransactionResponse
from MpesaSDK.transactions import TransactionStatus

D = Disposition


def resp(code=None, status=200, **body):
    if code:
        body["output_ResponseCode"] = code
    return ApiResponse(status, body)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("INS-0", D.SUCCESS),
        ("INS-1", D.RETRY),
        ("INS-9", D.RETRY),
        ("INS-10", D.DUPLICATE),
        ("INS-6", D.TERMINAL),
        ("INS-2006", D.TERMINAL),
        ("INS-2051", D.TERMINAL),
        ("INS-15", D.TERMINAL),
        ("INS-990", D.TERMINAL),
        ("INS-997", D.TERMINAL),
        ("INS-12345", D.TERMINAL),  # unknown codes must never be retried
    ],
)
def test_response_code_dispositions(code, expected):
    assert classify(resp(code)).disposition is expected


def test_every_documented_code_is_classified():
    for code in KNOWN_CODES:
        assert classify(resp(code)).disposition in set(D)


def test_transport_and_gateway_failures():
    net = classify(ApiResponse(None, network_error="ReadTimeout"))
    assert net.disposition is D.RETRY and net.ambiguous
    assert classify(ApiResponse(503)).disposition is D.RETRY
    assert classify(ApiResponse(503)).ambiguous
    rate = classify(ApiResponse(429))
    assert rate.disposition is D.RETRY and not rate.ambiguous
    auth = classify(ApiResponse(401))
    assert auth.disposition is D.RETRY and auth.refresh_session and not auth.ambiguous
    assert classify(ApiResponse(418)).disposition is D.TERMINAL


def test_http_401_with_openapi_body_is_not_an_auth_failure():
    # Docs map INS-6 "Transaction Failed" to HTTP 401: classify by code, not status.
    verdict = classify(resp("INS-6", status=401))
    assert verdict.disposition is D.TERMINAL and not verdict.refresh_session


def test_session_limits_request_a_fresh_session():
    assert classify(resp("INS-994")).refresh_session
    assert not classify(resp("INS-995")).refresh_session


def test_message_prefers_server_description():
    assert classify(resp("INS-2006", output_ResponseDesc="Low funds")).message == "Low funds"
    assert classify(resp("INS-2006")).message == "Insufficient balance"


def make_audit(**kw):
    base = dict(
        transaction_id="tp1",
        transaction_type="c2b_single_stage",
        amount="10",
        currency="LSL",
        recipient="ORG001",
        payer="26658123456",
        timestamp="2026-01-01T00:00:00.000+00:00",
        status=TransactionStatus.QUEUED,
        request_payload={"input_Amount": "10"},
    )
    return AuditTrail(**{**base, **kw})


def test_audit_log_contains_the_required_fields_and_is_json_safe():
    log = make_audit().log
    for key in (
        "amount",
        "recipient",
        "timestamp",
        "status",
        "retry_count",
        "mpesa_api_response",
        "request_payload",
    ):
        assert key in log
    assert log["status"] == "queued"
    json.dumps(log)


def test_audit_round_trip():
    audit = make_audit(mpesa_api_response={"a": 1}, retry_count=2)
    assert AuditTrail.from_dict(audit.to_dict()) == audit
    assert AuditTrail.from_dict(json.loads(audit.to_json())) == audit


def test_response_round_trip_and_ok_semantics():
    failure = TransactionFailure("INS-2006", "Insufficient balance", http_status=422)
    for status, ok in [
        (TransactionStatus.QUEUED, True),
        (TransactionStatus.PENDING, True),
        (TransactionStatus.SUCCEEDED, True),
        (TransactionStatus.FAILED, False),
        (TransactionStatus.UNKNOWN, False),
    ]:
        r = TransactionResponse(status, make_audit(status=status), None if ok else failure)
        assert r.ok is ok
        assert TransactionResponse.from_dict(r.to_dict()) == r


def test_raise_for_error():
    from MpesaSDK import TransactionError

    bad = TransactionResponse(
        TransactionStatus.FAILED, make_audit(), TransactionFailure("INS-6", "Transaction Failed")
    )
    with pytest.raises(TransactionError, match="Transaction Failed") as exc:
        bad.raise_for_error()
    assert exc.value.code == "INS-6"
    TransactionResponse(TransactionStatus.SUCCEEDED, make_audit()).raise_for_error()
