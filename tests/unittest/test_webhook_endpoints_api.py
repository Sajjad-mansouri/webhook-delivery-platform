from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.api_key import APIKey
from app.models.tenant import Tenant
from app.security.api_keys import generate_api_key, hash_api_key
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest_asyncio.fixture
async def endpoint_api(
    tmp_path: Path,
) -> AsyncIterator[dict[str, Any]]:
    database_path = tmp_path / "hookrelay-test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
    )

    # Ensure all model tables are registered before creating the schema.
    from app import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    test_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    tenant_a_key = generate_api_key()
    tenant_b_key = generate_api_key()

    async with test_session_factory.begin() as db:
        tenant_a = Tenant(
            name="Tenant A",
            slug="tenant-a",
        )
        tenant_b = Tenant(
            name="Tenant B",
            slug="tenant-b",
        )
        db.add_all([tenant_a, tenant_b])
        await db.flush()

        tenant_a_id = tenant_a.id

        db.add_all(
            [
                APIKey(
                    tenant_id=tenant_a.id,
                    name="Test key A",
                    key_prefix=tenant_a_key[:16],
                    key_hash=hash_api_key(tenant_a_key),
                ),
                APIKey(
                    tenant_id=tenant_b.id,
                    name="Test key B",
                    key_prefix=tenant_b_key[:16],
                    key_hash=hash_api_key(tenant_b_key),
                ),
            ]
        )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with test_session_factory() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test",
        ) as client:
            yield {
                "client": client,
                "tenant_a_key": tenant_a_key,
                "tenant_b_key": tenant_b_key,
                "tenant_a_id": tenant_a_id,
                "session_factory": test_session_factory,
            }
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


class TestWebhookEndpointAPI:
    @pytest.mark.asyncio
    async def test_create_endpoint_assigns_authenticated_tenant(
        self,
        endpoint_api: dict[str, Any],
    ) -> None:
        client: AsyncClient = endpoint_api["client"]
        api_key: str = endpoint_api["tenant_a_key"]
        tenant_id: UUID = endpoint_api["tenant_a_id"]

        response = await client.post(
            "/api/v1/endpoints",
            headers={"X-API-Key": api_key},
            json={
                "url": "https://hooks.example.com/orders",
                "description": "Order events",
            },
        )

        assert response.status_code == 201

        body = response.json()
        assert body["tenant_id"] == str(tenant_id)
        assert body["url"] == "https://hooks.example.com/orders"
        assert body["description"] == "Order events"
        assert body["is_active"] is True
        assert "id" in body
        assert "created_at" in body

    @pytest.mark.asyncio
    async def test_tenant_cannot_list_another_tenants_endpoints(
        self,
        endpoint_api: dict[str, Any],
    ) -> None:
        client: AsyncClient = endpoint_api["client"]

        create_response = await client.post(
            "/api/v1/endpoints",
            headers={
                "X-API-Key": endpoint_api["tenant_a_key"],
            },
            json={
                "url": "https://hooks.example.com/private",
            },
        )
        assert create_response.status_code == 201

        response = await client.get(
            "/api/v1/endpoints",
            headers={
                "X-API-Key": endpoint_api["tenant_b_key"],
            },
        )

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_tenant_cannot_read_another_tenants_endpoint(
        self,
        endpoint_api: dict[str, Any],
    ) -> None:
        client: AsyncClient = endpoint_api["client"]

        create_response = await client.post(
            "/api/v1/endpoints",
            headers={
                "X-API-Key": endpoint_api["tenant_a_key"],
            },
            json={
                "url": "https://hooks.example.com/private",
            },
        )
        assert create_response.status_code == 201

        endpoint_id = create_response.json()["id"]

        response = await client.get(
            f"/api/v1/endpoints/{endpoint_id}",
            headers={
                "X-API-Key": endpoint_api["tenant_b_key"],
            },
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_missing_api_key_cannot_create_endpoint(
        self,
        endpoint_api: dict[str, Any],
    ) -> None:
        client: AsyncClient = endpoint_api["client"]

        response = await client.post(
            "/api/v1/endpoints",
            json={
                "url": "https://hooks.example.com/orders",
            },
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    @pytest.mark.asyncio
    async def test_update_endpoint_changes_only_supplied_fields(
        self,
        endpoint_api: dict[str, Any],
    ) -> None:
        client: AsyncClient = endpoint_api["client"]
        headers = {"X-API-Key": endpoint_api["tenant_a_key"]}

        create_response = await client.post(
            "/api/v1/endpoints",
            headers=headers,
            json={
                "url": "https://hooks.example.com/orders",
                "description": "Original description",
            },
        )
        assert create_response.status_code == 201

        endpoint_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/v1/endpoints/{endpoint_id}",
            headers=headers,
            json={"is_active": False},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["is_active"] is False
        assert body["description"] == "Original description"
        assert body["url"] == "https://hooks.example.com/orders"

    @pytest.mark.asyncio
    async def test_delete_endpoint_returns_no_content(
        self,
        endpoint_api: dict[str, Any],
    ) -> None:
        client: AsyncClient = endpoint_api["client"]
        headers = {"X-API-Key": endpoint_api["tenant_a_key"]}

        create_response = await client.post(
            "/api/v1/endpoints",
            headers=headers,
            json={
                "url": "https://hooks.example.com/orders",
            },
        )
        assert create_response.status_code == 201

        endpoint_id = create_response.json()["id"]

        delete_response = await client.delete(
            f"/api/v1/endpoints/{endpoint_id}",
            headers=headers,
        )

        assert delete_response.status_code == 204
        assert delete_response.content == b""

        get_response = await client.get(
            f"/api/v1/endpoints/{endpoint_id}",
            headers=headers,
        )

        assert get_response.status_code == 404
