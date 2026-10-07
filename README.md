# mpesa-sdk

Auditable, retrying client for the M-Pesa OpenAPI (C2B single stage, sync and async), with
optional Celery + Redis background processing.

```bash
pip install mpesa-sdk              # inline mode
pip install "mpesa-sdk[queue]"     # + Celery/Redis background mode
```

```python
import MpesaSDK as mp

provider = mp.config(
    {
        "market": mp.markets.LESOTHO,
        "api-key": "...",
        "public-key": "...",
        "live": False,
        "retry-strategy": {"retry-count": 3, "back-off": 5.0, "jitter": 2},
        "concurrency": False,
    }
)

response = await provider.initialize_transaction(
    type=mp.transactions.C2B_SINGLE_STAGE,
    payload={
        "amount": "10",
        "customer_msisdn": "26658123456",
        "service_provider_code": "000000",
        "reference": "T1234C",
        "description": "Shoes",
    },
)
db.insert("audit_logs", response.audit.log)  # always store this
if not response.ok:
    raise mp.TransactionError(response.err.message)
```

`initialize_transaction` never raises for payment problems (declines, validation errors, outages):
it returns a response whose `ok`, `status`, `err` and `audit` describe what happened. Programmer
errors (unknown transaction type) raise `ValueError`; bad configuration raises `ConfigError` at
`mp.config(...)` time.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `market` | required | `mp.markets.LESOTHO` etc. (or `"lesotho"`, `"vodacomLES"`, `"LES"`) |
| `api-key`, `public-key` | required | Portal API key and the platform RSA public key (base64 DER or PEM) |
| `live` | `False` | `False` = sandbox, `True` = production |
| `retry-strategy.retry-count` | 3 | Retries *after* the first attempt (so up to 4 attempts) |
| `retry-strategy.back-off` | 5.0 | Seconds before retry 1; doubles each retry (`max-back-off`, default 300, caps it) |
| `retry-strategy.jitter` | 2 | Random `0..jitter` seconds added to each delay |
| `concurrency` | `False` | `True` = process in Celery workers via Redis |
| `max-workers` | 4 | Worker concurrency (queue mode); max in-flight for `initialize_many` (inline) |
| `redis-url` | `redis://localhost:6379/0` | Celery broker and result backend |
| `audit-handler` | `None` | `callable(response)` (sync or async) run once per final outcome, in the worker in queue mode. A safety net so audit logs are persisted even if nobody awaits the result. Exceptions are logged, never propagated |
| `queue`, `celery-options` | `"mpesa"`, `{}` | Queue name; extra Celery settings |
| `request-timeout`, `session-ttl`, `session-warmup` | 30, 1800, 0 | HTTP timeout; session cache lifetime (match your portal setting); optional wait after new session (docs say up to 30s) |
| `base-url`, `origin` | `https://openapi.m-pesa.com`, `*` | Override if needed; `origin` must match the Trusted Sources in your application |

Unknown keys raise `ConfigError`, so typos fail at start-up. Keys may be `hyphen-case` or `snake_case`.

## The response and the audit trail

```python
response.ok  # True for succeeded / queued / pending; False for failed / unknown
response.status  # QUEUED, RETRYING, PENDING, SUCCEEDED, FAILED, UNKNOWN
response.err  # None, or TransactionFailure(code, message, retryable, http_status, needs_reconciliation)
response.audit.log  # JSON-safe dict, store as-is
```

`audit.log` contains: `amount`, `currency`, `recipient`, `payer`, `timestamp` (initiated, UTC ISO-8601),
`status`, `retry_count`, `mpesa_api_response` (last body received), `request_payload` (exact JSON sent),
plus `transaction_id`, `transaction_type`, `conversation_id`, `mpesa_transaction_id`, `completed_at`,
`callback` and `attempts` (one entry per try: time, HTTP status, code, disposition, message). It never
contains the API key or session key. `recipient` is whoever *receives* the funds (for C2B, the business
shortcode); the customer's number is `payer`. The trail contains the customer's MSISDN, so treat
the table as personal data.

Statuses that need care:
* **PENDING** (`*_ASYNC` types): M-Pesa accepted the request; the final result arrives at your callback listener.
* **UNKNOWN**: the outcome could not be determined (timeouts/5xx until retries ran out, or a duplicate-key
  answer on a retry meaning an earlier attempt landed). `err.needs_reconciliation` is `True`. Query the
  transaction status with the same `transaction_id` before charging again.

## What gets retried

| Situation | Behaviour |
|---|---|
| `INS-0` | success |
| `INS-1`, `INS-9`, network error/timeout, HTTP 5xx | retried; if exhausted, **UNKNOWN** |
| HTTP 429, gateway 401/403 with no OpenAPI body, session creation failure | retried (never reached the platform); gateway auth failures also drop the cached session |
| `INS-10` duplicate | never retried; on a retry it becomes **UNKNOWN** |
| Everything else (`INS-6`, `INS-15`, `INS-2006` insufficient balance, `INS-2051`, limits `INS-99x`, ...) | **terminal**, not retried |

The same `input_ThirdPartyConversationID` is sent on every attempt (generated if you do not supply one), so
M-Pesa can detect a payment that actually went through. **Transaction IDs must be unique**; in queue mode
the Celery task id equals the transaction id.

## Queue mode (Celery + Redis)

```python
provider = mp.config({..., "concurrency": True, "max-workers": 3, "redis-url": "redis://..."})
celery_app = provider.celery_app          # in your module, e.g. payments.py
```
```bash
celery -A payments.celery_app worker      # picks up max-workers and the queue name
```

* `initialize_transaction` validates, enqueues and returns immediately with `status=QUEUED`
  (`ok=True` means *accepted for processing*, not paid). Get the outcome with
  `await provider.wait_for(response.task_id, timeout=...)` or `await provider.get_result(id)` (`None` while running).
* Each task run performs **one attempt**; retries are scheduled by Celery with the computed delay, so a
  backing-off transaction does not hold a worker slot. State travels with the retry message.
* Credentials never enter Redis: workers build their provider from your module. Messages do include the
  payload (amount, MSISDN), so secure Redis (auth, TLS, network).
* Tasks are `acks_late`: a crashed worker's task is redelivered; M-Pesa's duplicate detection makes that safe
  and the outcome surfaces as UNKNOWN if the first run had already landed.
* Broker down at submit time returns `FAILED` / `QUEUE_UNAVAILABLE` (`retryable=True`) with an audit trail.

## Async results (`*_ASYNC`)

```python
cb = mp.MpesaProvider.handle_callback(request_body)  # dict / str / bytes
audit = cb.apply_to(mp.AuditTrail.from_dict(stored_log))  # SUCCEEDED / FAILED, stores the callback
return cb.ack()  # required reply to M-Pesa
```

## Development

```bash
pip install -e ".[dev]"
pytest                                   # 141 tests, no network, no Redis needed
REDIS_URL=redis://localhost:6379/15 pytest   # also runs the real Celery worker + Redis test
ruff check . && ruff format .
```
Tests use a scriptable fake of the M-Pesa API that decrypts the real RSA bearer tokens, so the
encryption, headers, URLs, retry sequencing and audit output are all exercised end to end.
