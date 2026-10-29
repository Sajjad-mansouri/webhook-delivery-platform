import uuid
from datetime import UTC, datetime

import pytest
from app.db.base import Base
from app.models.api_key import APIKey
from app.models.tenant import Tenant
from app.security.api_keys import generate_api_key, hash_api_key
from app.security.dependencies import get_current_tenant
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class TestCurrentTenantDependencyEdgeCases:
    @pytest.mark.asyncio
    async def test_revoked_api_key_is_rejected(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)

            async with Session() as db:
                tenant = Tenant(name="Tenant A", slug="tenant-a")
                raw_key = generate_api_key()
                db.add(tenant)
                await db.flush()
                db.add(
                    APIKey(
                        tenant_id=tenant.id,
                        name="Revoked key",
                        key_prefix=raw_key[:16],
                        key_hash=hash_api_key(raw_key),
                        revoked_at=datetime.now(UTC),
                    )
                )
                await db.commit()

            async with Session() as db:
                with pytest.raises(HTTPException) as exc_info:
                    await get_current_tenant(db=db, api_key=raw_key)
                assert exc_info.value.status_code == 401
        finally:
            await engine.dispose()

    @pytest.mark.asyncio
    async def test_key_for_missing_tenant_is_rejected(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            raw_key = generate_api_key()

            async with Session() as db:
                db.add(
                    APIKey(
                        tenant_id=uuid.uuid4(),
                        name="Orphaned key",
                        key_prefix=raw_key[:16],
                        key_hash=hash_api_key(raw_key),
                    )
                )
                await db.commit()

            async with Session() as db:
                with pytest.raises(HTTPException) as exc_info:
                    await get_current_tenant(db=db, api_key=raw_key)
                assert exc_info.value.status_code == 401
        finally:
            await engine.dispose()
