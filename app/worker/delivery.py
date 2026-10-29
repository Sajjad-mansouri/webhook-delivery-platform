import json
import uuid
from datetime import UTC, datetime

import httpx
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.event import Event
from app.models.webhook_endpoint import WebhookEndpoint
from app.security.webhook_signatures import (
    create_signature_headers,
    decrypt_signing_secret,
)
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

    # 1. Load the delivery data and verify lease ownership.
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

        if endpoint.signing_secret_encrypted is None:
            raise RuntimeError(
                f"Endpoint {endpoint.id} has no signing secret configured."
            )

        # Copy the values needed for the request while the session is open.
        url = endpoint.url
        event_id = event.id
        event_type = event.event_type
        event_payload = event.payload
        encrypted_secret = endpoint.signing_secret_encrypted

    # 2. Prepare and sign the exact bytes that will be sent.
    #
    # Keep this outside the database session so no transaction is held
    # open during encryption or network operations.
    try:
        signing_secret = decrypt_signing_secret(encrypted_secret)
    except (ValueError, RuntimeError):
        # A configuration/decryption failure is not an HTTP delivery
        # failure. Raise it so the worker can report the operational
        # problem. The lease recovery mechanism can recover this job.
        raise

    body = json.dumps(
        {
            "id": str(event_id),
            "event_type": event_type,
            "payload": event_payload,
        },
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    signature_headers = create_signature_headers(
        signing_secret,
        body,
    )

    # 3. Perform network I/O without holding a database transaction.
    status_code: int | None = None
    error_message: str | None = None
    succeeded = False
    retryable = False

    try:
        response = await http_client.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-HookRelay-Event-ID": str(event_id),
                **signature_headers,
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

    # 4. Finalize only if this worker still owns the lease.
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
            # Another worker may have recovered or reclaimed the delivery.
            # Never let this stale worker overwrite the current state.
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
            # Recovery may already have finalized this attempt.
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
