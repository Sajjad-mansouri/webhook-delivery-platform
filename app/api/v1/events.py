from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.errors import AppError
from app.models.event import Event
from app.models.tenant import Tenant
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
    statement = select(Event).where(
        Event.tenant_id == tenant.id,
        Event.idempotency_key == idempotency_key,
    )

    result = await db.execute(statement)
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

    db.add(event)

    try:
        await db.commit()
    except IntegrityError:
        # Another concurrent request may have inserted this key
        # after our initial SELECT.
        await db.rollback()

        result = await db.execute(statement)
        existing_event = result.scalar_one_or_none()

        if existing_event is None:
            # This was not the expected idempotency race.
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
