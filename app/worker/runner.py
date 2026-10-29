import httpx
from app.worker.delivery import execute_delivery
from app.worker.queue import (
    claim_deliveries,
    recover_expired_deliveries,
)
from app.worker.retry import DEFAULT_MAX_ATTEMPTS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def process_delivery_batch(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    *,
    batch_size: int = 10,
    lease_seconds: int = 60,
) -> int:
    # Recover jobs left behind by crashed or stalled workers.
    await recover_expired_deliveries(
        session_factory,
        batch_size=100,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    )

    deliveries = await claim_deliveries(
        session_factory,
        batch_size=batch_size,
        lease_seconds=lease_seconds,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    )

    for delivery in deliveries:
        # Copy these values before passing control to the executor.
        delivery_id = delivery.id
        lease_token = delivery.lease_token

        if lease_token is None:
            raise RuntimeError(f"Claimed delivery {delivery_id} has no lease token.")

        await execute_delivery(
            session_factory,
            http_client,
            delivery_id,
            lease_token,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )

    return len(deliveries)
