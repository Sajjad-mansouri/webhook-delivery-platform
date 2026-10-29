from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.errors import AppError
from app.models.delivery import Delivery
from app.models.endpoint_subscription import EndpointSubscription
from app.models.event import Event
from app.models.tenant import Tenant
from app.models.webhook_endpoint import WebhookEndpoint
from app.schemas.event import EventCreate, EventRead
from app.security.dependencies import get_current_tenant

router = APIRouter(
    prefix="/api/v1/events",
    tags=["Events"],
)

DatabaseSession = Annotated[
    AsyncSession,
    Depends(get_db),
]

AuthenticatedTenant = Annotated[
    Tenant,
    Depends(get_current_tenant),
]


@router.post(
    "",
    response_model=EventRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_event(
    payload: EventCreate,
    response: Response,
    db: DatabaseSession,
    tenant: AuthenticatedTenant,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=255),
    ],
) -> Event:
    event_statement = select(Event).where(
        Event.tenant_id == tenant.id,
        Event.idempotency_key == idempotency_key,
    )

    result = await db.execute(event_statement)
    existing_event = result.scalar_one_or_none()

    if existing_event is not None:
        if (
            existing_event.event_type != payload.event_type
            or existing_event.payload != payload.payload
        ):
            raise AppError(
                status_code=409,
                code="idempotency_key_conflict",
                message=(
                    "This idempotency key was already used for a different event."
                ),
            )

        response.status_code = status.HTTP_200_OK
        return existing_event

    event = Event(
        tenant_id=tenant.id,
        event_type=payload.event_type,
        payload=payload.payload,
        idempotency_key=idempotency_key,
    )

    try:
        # Persist the event inside the current transaction so its ID
        # is available before creating delivery jobs.
        db.add(event)
        await db.flush()

        # Find active endpoints belonging to this tenant that
        # subscribe to this event type.
        subscriptions_statement = (
            select(EndpointSubscription.endpoint_id)
            .join(
                WebhookEndpoint,
                WebhookEndpoint.id == EndpointSubscription.endpoint_id,
            )
            .where(
                WebhookEndpoint.tenant_id == tenant.id,
                WebhookEndpoint.is_active.is_(True),
                EndpointSubscription.event_type == payload.event_type,
            )
        )

        subscription_result = await db.execute(subscriptions_statement)
        endpoint_ids = subscription_result.scalars().all()

        # Create one pending delivery job per matching endpoint.
        for endpoint_id in endpoint_ids:
            db.add(
                Delivery(
                    event_id=event.id,
                    endpoint_id=endpoint_id,
                    status="pending",
                    attempts=0,
                )
            )

        # Commit the event and all initial delivery jobs atomically.
        await db.commit()

    except IntegrityError:
        await db.rollback()

        # A concurrent request may have inserted the same
        # tenant-scoped idempotency key.
        result = await db.execute(event_statement)
        existing_event = result.scalar_one_or_none()

        if existing_event is None:
            # The integrity error was caused by something other
            # than a duplicate event.
            raise

        if (
            existing_event.event_type != payload.event_type
            or existing_event.payload != payload.payload
        ):
            raise AppError(
                status_code=409,
                code="idempotency_key_conflict",
                message=(
                    "This idempotency key was already used for a different event."
                ),
            ) from None

        response.status_code = status.HTTP_200_OK
        return existing_event

    await db.refresh(event)
    return event
