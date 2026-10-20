# mpesa-sdk

Auditable, retrying client for the M-Pesa OpenAPI (C2B, B2C and B2B single stage plus reversals, sync and async), with
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

## Supported transactions

| `mp.transactions.` | Endpoint | Funds flow | Final result |
|---|---|---|---|
| `C2B_SINGLE_STAGE` | `c2bPayment/singleStage/` | customer -> business | in the response |
| `C2B_SINGLE_STAGE_ASYNC` | same | customer -> business | by callback (`PENDING` first) |
| `B2C_SINGLE_STAGE` | `b2cPayment/` | business -> customer | in the response |
| `B2C_SINGLE_STAGE_ASYNC` | same | business -> customer | by callback (`PENDING` first) |
| `B2B_SINGLE_STAGE` | `b2bPayment/` | business -> business | in the response |
| `B2B_SINGLE_STAGE_ASYNC` | same | business -> business | by callback (`PENDING` first) |
| `REVERSAL` | `reversal/` (**PUT**) | back to the original initiator | in the response |
| `REVERSAL_ASYNC` | same | back to the original initiator | by callback (`PENDING` first) |

B2C payload (friendly keys; the raw `input_*` names also work):

```python
response = await provider.initialize_transaction(
    type=mp.transactions.B2C_SINGLE_STAGE,
    payload={
        "amount": "250.00",
        "customer_msisdn": "26658123456",  # who gets paid
        "service_provider_code": "000000",  # your shortcode
        "reference": "SAL2026",
        "description": "Salary payment",
    },
)
```

In the audit trail `recipient` is whoever **receives** the money: the shortcode for C2B, the
customer's number for B2C (and `payer` is the other party). The B2C description is sent as
`input_PaymentItemsDesc` (C2B uses `input_PurchasedItemsDesc`); `description` works for both.
Payouts move money out, so the retry rules below matter even more: one idempotency key per
payout, and `UNKNOWN` means *reconcile before paying again*. See `examples/b2c_single_stage.py`.

B2B payload: two shortcodes and no phone number. `primary_party_code` is debited,
`receiver_party_code` is credited (audit: `payer` / `recipient`). Paying a shortcode to itself is
rejected locally. See `examples/b2b_single_stage.py`.

```python
payload = {
    "amount": "1500.00",
    "primary_party_code": "ORG002",
    "receiver_party_code": "ORG001",
    "reference": "INV0042",
    "description": "Stock purchase",
}
```

Reversals undo a successful transaction by its **M-Pesa transaction id** (the
`mpesa_transaction_id` in the audit log you stored when you made it; for `*_ASYNC` payments it is
filled in once the callback arrives; it is not your own idempotency key):

```python
undo = await provider.initialize_transaction(
    type=mp.transactions.REVERSAL,
    payload={
        "transaction_id": paid_audit["mpesa_transaction_id"],
        "service_provider_code": "ORG001",  # who the funds go back to
        "amount": "25",
    },  # omit for a full reversal
)
undo.audit.log["related_transaction_id"]  # the transaction that was reversed
```

A reversal has no currency, reference or description, so passing them is a validation error. In its
audit trail `amount` is `None` for a full reversal, `recipient` is the shortcode, and `payer` is
`None` (the call does not name the party the money is taken from). Reversals follow the same retry
rules, with their own idempotency key. See `examples/reversal.py`.

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
pytest                                   # ~245 tests, ~2s, no network or Redis needed
REDIS_URL=redis://localhost:6379/15 pytest   # also runs the real Celery worker + Redis test
ruff check . && ruff format .
```
Tests use a scriptable fake of the M-Pesa API that decrypts the real RSA bearer tokens, so the
encryption, headers, URLs, retry sequencing and audit output are all exercised end to end.

## License

MIT. Free for personal and commercial use; see `LICENSE`.
