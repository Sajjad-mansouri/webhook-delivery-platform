from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.api_key import APIKey
from app.models.tenant import Tenant
from app.security.api_keys import hash_api_key


async def get_current_tenant(
    db: Annotated[AsyncSession, Depends(get_db)],
    api_key: Annotated[
        str | None,
        Header(
            alias="X-API-Key",
        ),
    ] = None,
) -> Tenant:

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization required."
        )

    statement = select(APIKey).where(
        APIKey.key_hash == hash_api_key(api_key), APIKey.revoked_at.is_(None)
    )

    result = await db.execute(statement)
    stored_key = result.scalar_one_or_none()
    if stored_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization required."
        )
    tenant = await db.get(Tenant, stored_key.tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization required."
        )

    return tenant
