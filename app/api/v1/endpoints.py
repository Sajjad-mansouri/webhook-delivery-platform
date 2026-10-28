from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.tenant import Tenant
from app.models.webhook_endpoint import WebhookEndpoint
from app.schemas.webhook_endpoint import (
    EndpointCreate,
    EndpointRead,
    EndpointUpdate,
)
from app.security.dependencies import get_current_tenant

router = APIRouter(
    prefix="/api/v1/endpoints",
    tags=["Webhook endpoints"],
)

DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
AuthenticatedTenant = Annotated[Tenant, Depends(get_current_tenant)]


@router.post(
    "",
    response_model=EndpointRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_endpoint(
    payload: EndpointCreate,
    db: DatabaseSession,
    current_tenant: AuthenticatedTenant,
) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        tenant_id=current_tenant.id,
        url=str(payload.url),
        description=payload.description,
    )

    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)

    return endpoint


@router.get(
    "",
    response_model=list[EndpointRead],
)
async def list_endpoints(
    db: DatabaseSession,
    current_tenant: AuthenticatedTenant,
) -> list[WebhookEndpoint]:
    statement = (
        select(WebhookEndpoint)
        .where(WebhookEndpoint.tenant_id == current_tenant.id)
        .order_by(WebhookEndpoint.created_at.desc())
    )

    result = await db.execute(statement)
    return list(result.scalars().all())


@router.get(
    "/{endpoint_id}",
    response_model=EndpointRead,
)
async def get_endpoint(
    endpoint_id: UUID,
    db: DatabaseSession,
    current_tenant: AuthenticatedTenant,
) -> WebhookEndpoint:
    statement = select(WebhookEndpoint).where(
        WebhookEndpoint.id == endpoint_id,
        WebhookEndpoint.tenant_id == current_tenant.id,
    )

    result = await db.execute(statement)
    endpoint = result.scalar_one_or_none()

    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found.",
        )

    return endpoint


@router.patch(
    "/{endpoint_id}",
    response_model=EndpointRead,
)
async def update_endpoint(
    endpoint_id: UUID,
    payload: EndpointUpdate,
    db: DatabaseSession,
    current_tenant: AuthenticatedTenant,
) -> WebhookEndpoint:
    statement = select(WebhookEndpoint).where(
        WebhookEndpoint.id == endpoint_id,
        WebhookEndpoint.tenant_id == current_tenant.id,
    )

    result = await db.execute(statement)
    endpoint = result.scalar_one_or_none()

    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found.",
        )

    changes = payload.model_dump(exclude_unset=True)

    if "url" in changes:
        if payload.url is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The url field cannot be null.",
            )
        endpoint.url = str(payload.url)

    if "description" in changes:
        endpoint.description = payload.description

    if "is_active" in changes:
        if payload.is_active is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The is_active field cannot be null.",
            )
        endpoint.is_active = payload.is_active

    await db.commit()
    await db.refresh(endpoint)

    return endpoint


@router.delete(
    "/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_endpoint(
    endpoint_id: UUID,
    db: DatabaseSession,
    current_tenant: AuthenticatedTenant,
) -> Response:
    statement = select(WebhookEndpoint).where(
        WebhookEndpoint.id == endpoint_id,
        WebhookEndpoint.tenant_id == current_tenant.id,
    )

    result = await db.execute(statement)
    endpoint = result.scalar_one_or_none()

    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found.",
        )

    await db.delete(endpoint)
    await db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)
