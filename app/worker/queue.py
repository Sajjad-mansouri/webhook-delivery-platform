import uuid
from datetime import UTC, datetime, timedelta

from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.worker.retry import (
    DEFAULT_MAX_ATTEMPTS,
    next_retry_at,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def claim_deliveries(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = 10,
    lease_seconds: int = 60,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[Delivery]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")

    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1.")

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")

    now = datetime.now(UTC)
    claimed: list[Delivery] = []

    async with session_factory.begin() as db:
        statement = (
            select(Delivery)
            .where(
                Delivery.status == "pending",
                Delivery.available_at <= now,
                Delivery.attempts < max_attempts,
            )
            .order_by(
                Delivery.available_at,
                Delivery.created_at,
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        result = await db.execute(statement)
        deliveries = list(result.scalars().all())

        for delivery in deliveries:
            delivery.status = "processing"
            delivery.attempts += 1
            delivery.lease_token = uuid.uuid4()
            delivery.locked_until = now + timedelta(seconds=lease_seconds)

            db.add(
                DeliveryAttempt(
                    delivery_id=delivery.id,
                    attempt_number=delivery.attempts,
                    outcome="processing",
                    started_at=now,
                )
            )

            claimed.append(delivery)

    # Requires expire_on_commit=False in the sessionmaker.
    return claimed


async def recover_expired_deliveries(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    batch_size: int = 100,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> int:
    """
    Recover deliveries whose worker leases have expired.

    Returns the number of deliveries recovered. An abandoned attempt
    consumes an attempt because the claim already incremented attempts.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")

    now = datetime.now(UTC)
    recovered_count = 0

    async with session_factory.begin() as db:
        statement = (
            select(Delivery)
            .where(
                Delivery.status == "processing",
                Delivery.locked_until.is_not(None),
                Delivery.locked_until <= now,
            )
            .order_by(Delivery.locked_until)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )

        result = await db.execute(statement)
        deliveries = list(result.scalars().all())

        for delivery in deliveries:
            attempt_result = await db.execute(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == delivery.id,
                    DeliveryAttempt.attempt_number == delivery.attempts,
                )
            )
            attempt = attempt_result.scalar_one_or_none()

            if attempt is None:
                raise RuntimeError(
                    "Missing attempt history for expired delivery "
                    f"{delivery.id}, attempt {delivery.attempts}."
                )

            # Only recover attempts that are still unfinished.
            if attempt.outcome == "processing":
                attempt.outcome = "abandoned"
                attempt.error_message = (
                    "Worker lease expired before the attempt was finalized."
                )
                attempt.finished_at = now

            if delivery.attempts >= max_attempts:
                delivery.status = "failed"
            else:
                delivery.status = "pending"
                delivery.available_at = next_retry_at(
                    delivery.attempts,
                    now=now,
                )

            delivery.locked_until = None
            delivery.lease_token = None
            recovered_count += 1

    return recovered_count
