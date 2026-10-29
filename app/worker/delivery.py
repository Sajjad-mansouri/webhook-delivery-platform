import uuid
from datetime import UTC, datetime

import httpx
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.event import Event
from app.models.webhook_endpoint import WebhookEndpoint
from app.worker.retry import (
    DEFAULT_MAX_ATTEMPTS,
    classify_http_status,
    next_retry_at,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def execute_delivery(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    delivery_id: uuid.UUID,
    lease_token: uuid.UUID,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")

    # 1. Load request data and verify lease ownership.
    async with session_factory() as db:
        result = await db.execute(
            select(Delivery, Event, WebhookEndpoint)
            .join(Event, Event.id == Delivery.event_id)
            .join(
                WebhookEndpoint,
                WebhookEndpoint.id == Delivery.endpoint_id,
            )
            .where(Delivery.id == delivery_id)
        )
        row = result.one_or_none()

        if row is None:
            return

        delivery, event, endpoint = row

        if delivery.status != "processing" or delivery.lease_token != lease_token:
            return

        attempt_number = delivery.attempts

        if attempt_number < 1:
            raise RuntimeError(f"Delivery {delivery_id} has an invalid attempt count.")

        url = endpoint.url
        event_id = event.id
        event_type = event.event_type
        event_payload = event.payload

    # 2. Perform network I/O without holding a database transaction.
    status_code: int | None = None
    error_message: str | None = None
    succeeded = False
    retryable = False

    try:
        response = await http_client.post(
            url,
            json={
                "id": str(event_id),
                "event_type": event_type,
                "payload": event_payload,
            },
            headers={
                "Content-Type": "application/json",
                "X-HookRelay-Event-ID": str(event_id),
            },
        )

        status_code = response.status_code
        decision = classify_http_status(status_code)
        succeeded = decision.reason == "success"
        retryable = decision.should_retry

        if not succeeded:
            error_message = f"Destination returned HTTP {status_code}."

    except httpx.TimeoutException:
        retryable = True
        error_message = "Request timed out."

    except httpx.RequestError:
        retryable = True
        error_message = "Network request failed."

    # 3. Finalize only if the lease still belongs to this worker.
    async with session_factory.begin() as db:
        result = await db.execute(
            select(Delivery).where(Delivery.id == delivery_id).with_for_update()
        )
        delivery = result.scalar_one_or_none()

        if (
            delivery is None
            or delivery.status != "processing"
            or delivery.lease_token != lease_token
        ):
            # Another worker may have recovered and reclaimed the job.
            return

        attempt_result = await db.execute(
            select(DeliveryAttempt).where(
                DeliveryAttempt.delivery_id == delivery_id,
                DeliveryAttempt.attempt_number == attempt_number,
            )
        )
        attempt = attempt_result.scalar_one_or_none()

        if attempt is None:
            raise RuntimeError(
                f"Missing attempt {attempt_number} for delivery {delivery_id}."
            )

        if attempt.outcome != "processing":
            # Do not overwrite an attempt already finalized by recovery.
            return

        now = datetime.now(UTC)

        attempt.status_code = status_code
        attempt.error_message = error_message
        attempt.finished_at = now

        if succeeded:
            attempt.outcome = "succeeded"
            delivery.status = "succeeded"

        elif retryable and attempt_number < max_attempts:
            attempt.outcome = "failed"
            delivery.status = "pending"
            delivery.available_at = next_retry_at(
                attempt_number,
                now=now,
            )

        else:
            attempt.outcome = "failed"
            delivery.status = "failed"

        delivery.locked_until = None
        delivery.lease_token = None
