"""Queue mode. Put this in e.g. payments.py, then:

celery -A payments.celery_app worker          # concurrency comes from "max-workers"
python payments.py                            # enqueue a transaction
"""

import asyncio

import MpesaSDK as mp

provider = mp.config(
    {
        "market": mp.markets.LESOTHO,
        "api-key": "your api key",
        "public-key": "your public key",
        "concurrency": True,
        "max-workers": 3,
        "redis-url": "redis://localhost:6379/0",
        "audit-handler": lambda r: print("persist me:", r.audit.log),  # runs in the worker
    }
)
celery_app = provider.celery_app  # what `celery -A` points at


async def main() -> None:
    queued = await provider.initialize_transaction(
        mp.transactions.C2B_SINGLE_STAGE,
        {
            "amount": 10,
            "msisdn": "26658123456",
            "shortcode": "000000",
            "reference": "T1234C",
            "description": "Shoes",
        },
    )
    print(queued.status)  # QUEUED: returned immediately, nothing sent to M-Pesa yet
    final = await provider.wait_for(queued.task_id, timeout=120)
    print(final.status, final.audit.retry_count)


if __name__ == "__main__":
    asyncio.run(main())
