import uuid
from datetime import UTC, datetime

import httpx
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.event import Event
from app.models.webhook_endpoint import WebhookEndpoint
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def execute_delivery(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    delivery_id: uuid.UUID,
) -> None:
    # Load the data needed for the network request.
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

        # Do not send a request for a job that is no longer processing.
        if delivery.status != "processing":
            return

        url = endpoint.url
        event_id = event.id
        event_type = event.event_type
        event_payload = event.payload
        attempt_number = delivery.attempts + 1

    # The database session is closed before network I/O.
    status_code: int | None = None
    error_message: str | None = None
    succeeded = False

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
        succeeded = 200 <= status_code < 300

        if not succeeded:
            error_message = f"Destination returned HTTP {status_code}."

    except httpx.TimeoutException:
        error_message = "Request timed out."

    except httpx.RequestError:
        error_message = "Network request failed."

    # Record the result in a new short-lived database transaction.
    async with session_factory.begin() as db:
        result = await db.execute(
            select(Delivery).where(Delivery.id == delivery_id).with_for_update()
        )
        delivery = result.scalar_one_or_none()

        if delivery is None or delivery.status != "processing":
            return

        now = datetime.now(UTC)

        delivery.attempts = attempt_number
        delivery.locked_until = None
        delivery.status = "succeeded" if succeeded else "pending"

        db.add(
            DeliveryAttempt(
                delivery_id=delivery.id,
                attempt_number=attempt_number,
                status_code=status_code,
                error_message=error_message,
                outcome="succeeded" if succeeded else "failed",
                finished_at=now,
            )
        )
