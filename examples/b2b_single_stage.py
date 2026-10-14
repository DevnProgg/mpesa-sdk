"""B2B single stage: pay another business from your business wallet (stock, bills, ad-hoc).

Run: python examples/b2b_single_stage.py   (set real keys first)

  primary_party_code   = the shortcode funds are taken FROM (your business)
  receiver_party_code  = the shortcode funds are paid TO (the other business)

Like any payout, a transaction that ends UNKNOWN (unresolved timeout) must be reconciled
before you pay again; the same transaction id is reused on every retry so M-Pesa can
detect repeats.
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
    response = await provider.initialize_transaction(
        type=mp.transactions.B2B_SINGLE_STAGE,
        payload={
            "amount": "1500.00",
            "primary_party_code": "000000",  # debited
            "receiver_party_code": "000001",  # credited
            "reference": "INV0042",
            "description": "Stock purchase",
            # "third_party_conversation_id": "inv-0042-payment",  # your own unique id
        },
    )
    audit_row = response.audit.log  # store this, success or failure
    # audit_row["payer"] is the primary party, audit_row["recipient"] the receiver

    if response.status is mp.TransactionStatus.UNKNOWN:
        raise RuntimeError(
            f"Reconcile {response.transaction_id} before retrying: {response.err.message}"
        )
    if not response.ok:
        raise mp.TransactionError(response.err.message, code=response.err.code)
    print(response.status, audit_row["mpesa_api_response"])


if __name__ == "__main__":
    asyncio.run(main())
