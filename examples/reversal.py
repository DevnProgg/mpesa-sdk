"""Reverse a successful transaction (full or partial).

Run: python examples/reversal.py   (set real keys first)

A reversal needs the *M-Pesa transaction id* of the original payment, which is
``audit_log["mpesa_transaction_id"]`` of the response you stored when you made it (for the
``*_ASYNC`` types it is filled in once the callback arrives). It is not your own
``third_party_conversation_id``.

The reversal gets its own idempotency key (``third_party_conversation_id``). Reuse the same one
if you re-submit the *same* reversal, so M-Pesa can recognise a repeat; and if it ends UNKNOWN
(unresolved timeout), reconcile before reversing again.
"""

import asyncio

import MpesaSDK as mp

provider = mp.config(
    {
        "market": mp.markets.LESOTHO,
        "api-key": "your api key",
        "public-key": "your public key",
        "live": False,
        "retry-strategy": {"retry-count": 3, "back-off": 5.0, "jitter": 2},
    }
)


async def main() -> None:
    original_audit = {"mpesa_transaction_id": "0000000000001"}  # load from your audit table

    response = await provider.initialize_transaction(
        type=mp.transactions.REVERSAL,
        payload={
            "transaction_id": original_audit["mpesa_transaction_id"],
            "service_provider_code": "000000",  # the organisation the funds go back to
            "amount": "25",  # leave this out to reverse the full amount
            # "third_party_conversation_id": "rev-for-order-1042",  # your own unique id
        },
    )
    audit_row = response.audit.log  # store this too
    # audit_row["related_transaction_id"] links it to the transaction being reversed;
    # audit_row["amount"] is None for a full reversal.

    if response.status is mp.TransactionStatus.UNKNOWN:
        raise RuntimeError(
            f"Reconcile {response.transaction_id} before retrying: {response.err.message}"
        )
    if not response.ok:
        raise mp.TransactionError(response.err.message, code=response.err.code)
    print(response.status, audit_row["mpesa_api_response"])


if __name__ == "__main__":
    asyncio.run(main())
