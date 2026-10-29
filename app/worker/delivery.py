import uuid
from datetime import UTC, datetime

import httpx
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.event import Event
from app.models.webhook_endpoint import WebhookEndpoint
from app.worker.retry import classify_http_status, next_retry_at
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAX_DELIVERY_ATTEMPTS = 8


async def execute_delivery(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    delivery_id: uuid.UUID,
    lease_token: uuid.UUID,
) -> None:
    """
    Execute one claimed delivery attempt.

    The worker must supply the lease token returned by the claim operation.
    The token prevents an old worker from finalizing a job reclaimed by
    another worker.

    Delivery.attempts is incremented during claiming, and the corresponding
    DeliveryAttempt row is created in the same claim transaction.
    """

    # ---------------------------------------------------------------
    # 1. Load the request data and verify that this worker owns the job.
    # ---------------------------------------------------------------
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
            # The job is no longer owned by this worker.
            return

        # Claiming must increment attempts before execution starts.
        attempt_number = delivery.attempts

        if attempt_number < 1:
            raise RuntimeError(f"Delivery {delivery_id} has an invalid attempt count.")

        # Snapshot the data while the session is open. Do not keep a
        # database transaction or session open during network I/O.
        url = endpoint.url
        event_id = event.id
        event_type = event.event_type
        event_payload = event.payload

        attempt_result = await db.execute(
            select(DeliveryAttempt).where(
                DeliveryAttempt.delivery_id == delivery_id,
                DeliveryAttempt.attempt_number == attempt_number,
            )
        )
        attempt_exists = attempt_result.scalar_one_or_none()

        if attempt_exists is None:
            raise RuntimeError(
                f"Attempt {attempt_number} for delivery {delivery_id} "
                "was not created during claiming."
            )

    # ---------------------------------------------------------------
    # 2. Send the HTTP request without holding a database transaction.
    # ---------------------------------------------------------------
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
        succeeded = 200 <= status_code < 300

        if not succeeded:
            decision = classify_http_status(status_code)
            retryable = decision.should_retry

            if retryable:
                error_message = f"Destination returned retryable HTTP {status_code}."
            else:
                error_message = f"Destination returned terminal HTTP {status_code}."

    except httpx.TimeoutException:
        retryable = True
        error_message = "Request timed out."

    except httpx.RequestError:
        retryable = True
        error_message = "Network request failed."

    # ---------------------------------------------------------------
    # 3. Finalize atomically, but only if this worker still owns the job.
    # ---------------------------------------------------------------
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
            # The lease expired or the job was reclaimed while the
            # request was in flight. Do not overwrite the new owner's
            # state or attempt history.
            return

        now = datetime.now(UTC)

        attempt_result = await db.execute(
            select(DeliveryAttempt).where(
                DeliveryAttempt.delivery_id == delivery_id,
                DeliveryAttempt.attempt_number == attempt_number,
            )
        )
        attempt = attempt_result.scalar_one_or_none()

        if attempt is None:
            raise RuntimeError(
                f"Attempt {attempt_number} for delivery {delivery_id} "
                "was not found during finalization."
            )

        # Update the attempt record created when the job was claimed.
        attempt.status_code = status_code
        attempt.error_message = error_message
        attempt.finished_at = now

        if succeeded:
            attempt.outcome = "succeeded"
            delivery.status = "succeeded"

        elif retryable and attempt_number < MAX_DELIVERY_ATTEMPTS:
            attempt.outcome = "failed"
            delivery.status = "pending"
            delivery.available_at = next_retry_at(
                attempt_number,
                now=now,
            )

        else:
            # Either this failure is terminal, or the retry budget
            # has been exhausted.
            attempt.outcome = "failed"
            delivery.status = "failed"

        # Release the lease after finalization.
        delivery.locked_until = None
        delivery.lease_token = None
