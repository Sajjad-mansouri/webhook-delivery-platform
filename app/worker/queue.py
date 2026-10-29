from datetime import UTC, datetime, timedelta

from app.models.delivery import Delivery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def claim_deliveries(
    db: AsyncSession,
    *,
    batch_size: int = 10,
    lease_seconds: int = 60,
) -> list[Delivery]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1")

    now = datetime.now(UTC)

    statement = (
        select(Delivery)
        .where(
            Delivery.status == "pending",
            Delivery.available_at <= now,
        )
        .order_by(Delivery.available_at, Delivery.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )

    result = await db.execute(statement)
    deliveries = list(result.scalars().all())

    for delivery in deliveries:
        delivery.status = "processing"
        delivery.locked_until = now + timedelta(seconds=lease_seconds)

    await db.commit()
    return deliveries
