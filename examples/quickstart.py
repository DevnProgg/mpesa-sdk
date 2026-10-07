"""Run: python examples/quickstart.py   (inline mode; set real keys first)"""

import asyncio

import MpesaSDK as mp

provider = mp.config(
    {
        "market": mp.markets.LESOTHO,
        "api-key": "your api key",
        "public-key": "your public key",
        "live": False,
        "retry-strategy": {"retry-count": 3, "back-off": 5.0, "jitter": 2},
        "audit-handler": lambda response: print("AUDIT", response.audit.to_json()),
    }
)


async def main() -> None:
    response = await provider.initialize_transaction(
        type=mp.transactions.C2B_SINGLE_STAGE,
        payload={
            "amount": "10.00",
            "customer_msisdn": "26658123456",
            "service_provider_code": "000000",
            "reference": "T1234C",
            "description": "Shoes",
        },
    )
    audit_row = response.audit.log  # store this, success or failure
    if not response.ok:
        raise mp.TransactionError(response.err.message, code=response.err.code)
    print(response.status, audit_row["mpesa_api_response"])


if __name__ == "__main__":
    asyncio.run(main())
