"""B2C single stage: pay a customer from your business wallet (salary, pay-out, refund).

Run: python examples/b2c_single_stage.py   (set real keys first)

Payouts move money *out*, so the SDK's safety rules matter most here:
  * the same ThirdPartyConversationID is sent on every retry, so M-Pesa can spot a repeat;
  * a timeout that cannot be resolved ends as UNKNOWN, never as a blind failure: reconcile
    (query the transaction status) before paying again.
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
        type=mp.transactions.B2C_SINGLE_STAGE,
        payload={
            "amount": "250.00",
            "customer_msisdn": "26658123456",  # who gets paid
            "service_provider_code": "000000",  # your business shortcode (funds come from here)
            "reference": "SAL2026",
            "description": "Salary payment",
            # "third_party_conversation_id": "payroll-2026-10-emp-117",  # your own unique id
        },
    )
    audit_row = response.audit.log  # store this, success or failure
    # audit_row["recipient"] is the customer, audit_row["payer"] is your shortcode

    if response.status is mp.TransactionStatus.UNKNOWN:
        # Do NOT simply pay again: the first attempt may have gone through.
        raise RuntimeError(
            f"Reconcile {response.transaction_id} before retrying: {response.err.message}"
        )
    if not response.ok:
        raise mp.TransactionError(response.err.message, code=response.err.code)
    print(response.status, audit_row["mpesa_api_response"])


if __name__ == "__main__":
    asyncio.run(main())
