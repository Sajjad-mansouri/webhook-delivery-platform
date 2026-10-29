import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.errors import AppError
from app.models.endpoint_subscription import EndpointSubscription
from app.models.tenant import Tenant
from app.models.webhook_endpoint import WebhookEndpoint
from app.schemas.endpoint_subscription import (
    SubscriptionCreate,
    SubscriptionRead,
)
from app.security.dependencies import get_current_tenant

router = APIRouter(
    prefix="/api/v1/endpoints",
    tags=["Subscriptions"],
)

DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
AuthenticatedTenant = Annotated[Tenant, Depends(get_current_tenant)]


@router.post(
    "/{endpoint_id}/subscriptions",
    response_model=SubscriptionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    endpoint_id: uuid.UUID,
    payload: SubscriptionCreate,
    db: DatabaseSession,
    tenant: AuthenticatedTenant,
) -> EndpointSubscription:
    endpoint_result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.tenant_id == tenant.id,
        )
    )
    endpoint = endpoint_result.scalar_one_or_none()

    if endpoint is None:
        raise AppError(
            status_code=404,
            code="endpoint_not_found",
            message="Webhook endpoint not found.",
        )

    existing_result = await db.execute(
        select(EndpointSubscription).where(
            EndpointSubscription.endpoint_id == endpoint_id,
            EndpointSubscription.event_type == payload.event_type,
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        raise AppError(
            status_code=409,
            code="subscription_exists",
            message="This endpoint is already subscribed to this event type.",
        )

    subscription = EndpointSubscription(
        endpoint_id=endpoint_id,
        event_type=payload.event_type,
    )
    db.add(subscription)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError(
            status_code=409,
            code="subscription_exists",
            message="This endpoint is already subscribed to this event type.",
        ) from None

    await db.refresh(subscription)
    return subscription
