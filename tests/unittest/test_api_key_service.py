import hashlib

import pytest
from app.db.base import Base
from app.models.api_key import APIKey
from app.models.tenant import Tenant
from app.security.api_keys import API_KEY_PREFIX, api_key_matches, hash_api_key
from app.services.api_keys import create_api_key
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class TestCreateAPIKeyService:
    @pytest.mark.asyncio
    async def test_persists_key_metadata_and_returns_matching_plaintext_key(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as db:
                tenant = Tenant(name="Tenant A", slug="tenant-a")
                db.add(tenant)
                await db.flush()

                record, raw_key = await create_api_key(
                    db, tenant_id=tenant.id, name="Integration key"
                )

                assert raw_key.startswith(API_KEY_PREFIX)
                assert record.tenant_id == tenant.id
                assert record.name == "Integration key"
                assert record.key_prefix == raw_key[:16]
                assert (
                    record.key_hash
                    == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
                )
                assert api_key_matches(raw_key, record.key_hash)
                assert raw_key != record.key_hash

                await db.commit()
                key_id = record.id

            async with Session() as db:
                persisted = await db.get(APIKey, key_id)
                assert persisted is not None
                assert persisted.tenant_id == tenant.id
                assert persisted.name == "Integration key"
                assert persisted.key_prefix == raw_key[:16]
                assert persisted.key_hash == hash_api_key(raw_key)
                assert not hasattr(persisted, "key")
        finally:
            await engine.dispose()
