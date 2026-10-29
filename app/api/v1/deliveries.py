import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.errors import AppError
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.event import Event
from app.models.tenant import Tenant
from app.schemas.delivery import (
    DeliveryAttemptRead,
    DeliveryRead,
    DeliveryReplayRead,
)
from app.security.dependencies import get_current_tenant

router = APIRouter(prefix="/api/v1/deliveries", tags=["Deliveries"])


async def _get_tenant_delivery(
    db: AsyncSession,
    *,
    delivery_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Delivery:
    result = await db.execute(
        select(Delivery)
        .join(Event, Event.id == Delivery.event_id)
        .where(
            Delivery.id == delivery_id,
            Event.tenant_id == tenant_id,
        )
    )
    delivery = result.scalar_one_or_none()

    if delivery is None:
        raise AppError(
            code="not_found",
            message="Delivery not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return delivery


async def _build_delivery_read(
    db: AsyncSession,
    delivery: Delivery,
) -> DeliveryRead:
    result = await db.execute(
        select(DeliveryAttempt)
        .where(DeliveryAttempt.delivery_id == delivery.id)
        .order_by(DeliveryAttempt.attempt_number.asc())
    )
    attempts = result.scalars().all()

    return DeliveryRead(
        id=delivery.id,
        event_id=delivery.event_id,
        endpoint_id=delivery.endpoint_id,
        replay_of_delivery_id=delivery.replay_of_delivery_id,
        status=delivery.status,
        attempts=delivery.attempts,
        available_at=delivery.available_at,
        locked_until=delivery.locked_until,
        created_at=delivery.created_at,
        attempt_history=[
            DeliveryAttemptRead.model_validate(attempt) for attempt in attempts
        ],
    )


@router.get("", response_model=list[DeliveryRead])
async def list_deliveries(
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[DeliveryRead]:
    result = await db.execute(
        select(Delivery)
        .join(Event, Event.id == Delivery.event_id)
        .where(Event.tenant_id == tenant.id)
        .order_by(Delivery.created_at.desc(), Delivery.id.desc())
        .limit(limit)
        .offset(offset)
    )
    deliveries = result.scalars().all()

    return [await _build_delivery_read(db, delivery) for delivery in deliveries]


@router.get("/{delivery_id}", response_model=DeliveryRead)
async def get_delivery(
    delivery_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> DeliveryRead:
    delivery = await _get_tenant_delivery(
        db,
        delivery_id=delivery_id,
        tenant_id=tenant.id,
    )

    return await _build_delivery_read(db, delivery)


@router.post(
    "/{delivery_id}/replay",
    response_model=DeliveryReplayRead,
    status_code=status.HTTP_201_CREATED,
)
async def replay_delivery(
    delivery_id: uuid.UUID,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant: Annotated[Tenant, Depends(get_current_tenant)],
) -> DeliveryReplayRead:
    # Lock the source delivery on databases that support row-level locking.
    result = await db.execute(
        select(Delivery)
        .join(Event, Event.id == Delivery.event_id)
        .where(
            Delivery.id == delivery_id,
            Event.tenant_id == tenant.id,
        )
        .with_for_update()
    )
    original = result.scalar_one_or_none()

    if original is None:
        raise AppError(
            code="not_found",
            message="Delivery not found.",
            status_code=status.HTTP_404_NOT_FOUND,
        )

    if original.status != "failed":
        raise AppError(
            code="invalid_delivery_state",
            message="Only failed deliveries can be replayed.",
            status_code=status.HTTP_409_CONFLICT,
        )

    now = datetime.now(UTC)

    replay = Delivery(
        event_id=original.event_id,
        endpoint_id=original.endpoint_id,
        replay_of_delivery_id=original.id,
        status="pending",
        attempts=0,
        available_at=now,
        locked_until=None,
        lease_token=None,
    )

    # Keep the original delivery and its attempt history unchanged.
    db.add(replay)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(replay)

    response.headers["Location"] = f"/api/v1/deliveries/{replay.id}"

    return DeliveryReplayRead(
        id=replay.id,
        event_id=replay.event_id,
        endpoint_id=replay.endpoint_id,
        replay_of_delivery_id=replay.replay_of_delivery_id,
        status=replay.status,
        attempts=replay.attempts,
        available_at=replay.available_at,
        created_at=replay.created_at,
    )
