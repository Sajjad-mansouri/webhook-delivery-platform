# tests/integration/test_delivery_api.py

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
import pytest
import pytest_asyncio
from app import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.event import Event
from app.models.tenant import Tenant
from app.models.webhook_endpoint import WebhookEndpoint
from app.security.dependencies import get_current_tenant
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
def database_url(request, tmp_path) -> str:
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'delivery-api.db'}"

    url = os.environ.get("HOOKRELAY_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("HOOKRELAY_TEST_POSTGRES_URL is not configured.")

    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("HOOKRELAY_TEST_POSTGRES_URL must use 'postgresql+asyncpg://'.")

    return url


@pytest_asyncio.fixture
async def session_factory(database_url):
    # This fixture drops and recreates all tables.
    # Never point it at a development or production database.
    engine = create_async_engine(database_url)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

        async with factory.begin() as session:
            session.add_all(
                [
                    Tenant(name="Tenant A", slug="tenant-a"),
                    Tenant(name="Tenant B", slug="tenant-b"),
                ]
            )

        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def override_get_current_tenant(
        db: Annotated[AsyncSession, Depends(get_db)],
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Tenant:
        tenant_slugs = {
            "tenant-a-test-key": "tenant-a",
            "tenant-b-test-key": "tenant-b",
        }

        slug = tenant_slugs.get(x_api_key or "")
        if slug is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid API key.",
            )

        result = await db.execute(select(Tenant).where(Tenant.slug == slug))
        tenant = result.scalar_one_or_none()

        if tenant is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid API key.",
            )

        return tenant

    previous_overrides = dict(fastapi_app.dependency_overrides)

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_current_tenant] = override_get_current_tenant

    try:
        transport = httpx.ASGITransport(app=fastapi_app)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as test_client:
            yield test_client
    finally:
        fastapi_app.dependency_overrides.clear()
        fastapi_app.dependency_overrides.update(previous_overrides)


class DeliveryAPITestHelpers:
    base_url = "/api/v1/deliveries"

    @staticmethod
    def auth(tenant: str = "a") -> dict[str, str]:
        return {"X-API-Key": f"tenant-{tenant}-test-key"}

    @staticmethod
    async def get_tenant_id(
        session_factory: async_sessionmaker[AsyncSession],
        slug: str,
    ) -> uuid.UUID:
        async with session_factory() as session:
            result = await session.execute(select(Tenant.id).where(Tenant.slug == slug))
            tenant_id = result.scalar_one_or_none()

            if tenant_id is None:
                raise AssertionError(f"Tenant {slug!r} does not exist.")

            return tenant_id

    @classmethod
    async def create_delivery(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tenant_slug: str = "tenant-a",
        status: str = "failed",
        attempts: int = 2,
    ) -> uuid.UUID:
        """Create a delivery and realistic attempt history."""
        tenant_id = await cls.get_tenant_id(
            session_factory,
            tenant_slug,
        )
        now = datetime.now(UTC)

        async with session_factory.begin() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant_id,
                url="https://receiver.example.test/webhook",
                description="Delivery API integration test",
                is_active=True,
            )
            event = Event(
                tenant_id=tenant_id,
                event_type="order.created",
                payload={"order_id": str(uuid.uuid4())},
                idempotency_key=f"delivery-api-{uuid.uuid4()}",
            )

            session.add_all([endpoint, event])
            await session.flush()

            delivery = Delivery(
                event_id=event.id,
                endpoint_id=endpoint.id,
                status=status,
                attempts=attempts,
                available_at=now,
                locked_until=None,
                lease_token=None,
                created_at=now,
            )
            session.add(delivery)
            await session.flush()

            # Insert in reverse order to verify that the API sorts history.
            for attempt_number in reversed(range(1, attempts + 1)):
                started_at = now - timedelta(minutes=attempt_number)
                is_success = status == "succeeded" and attempt_number == attempts

                session.add(
                    DeliveryAttempt(
                        delivery_id=delivery.id,
                        attempt_number=attempt_number,
                        status_code=200 if is_success else 500,
                        error_message=(
                            None if is_success else f"Test failure {attempt_number}"
                        ),
                        outcome="succeeded" if is_success else "failed",
                        started_at=started_at,
                        finished_at=started_at + timedelta(seconds=2),
                    )
                )

            await session.flush()
            return delivery.id


class TestDeliveryListAPI(DeliveryAPITestHelpers):
    @pytest.mark.asyncio
    async def test_list_deliveries_returns_only_current_tenants_deliveries(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant_a_id_1 = await self.create_delivery(
            session_factory,
            tenant_slug="tenant-a",
        )
        tenant_a_id_2 = await self.create_delivery(
            session_factory,
            tenant_slug="tenant-a",
        )
        await self.create_delivery(
            session_factory,
            tenant_slug="tenant-b",
        )

        response = await client.get(
            self.base_url,
            headers=self.auth("a"),
        )

        assert response.status_code == 200

        body = response.json()
        returned_ids = {item["id"] for item in body}

        assert returned_ids == {
            str(tenant_a_id_1),
            str(tenant_a_id_2),
        }
        assert len(body) == 2

    @pytest.mark.asyncio
    async def test_list_deliveries_supports_limit_and_offset(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        for _ in range(5):
            await self.create_delivery(session_factory)

        first_response = await client.get(
            self.base_url,
            params={"limit": 2, "offset": 0},
            headers=self.auth("a"),
        )
        second_response = await client.get(
            self.base_url,
            params={"limit": 2, "offset": 2},
            headers=self.auth("a"),
        )
        final_response = await client.get(
            self.base_url,
            params={"limit": 2, "offset": 4},
            headers=self.auth("a"),
        )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert final_response.status_code == 200

        first_ids = {item["id"] for item in first_response.json()}
        second_ids = {item["id"] for item in second_response.json()}
        final_ids = {item["id"] for item in final_response.json()}

        assert len(first_ids) == 2
        assert len(second_ids) == 2
        assert len(final_ids) == 1

        assert first_ids.isdisjoint(second_ids)
        assert first_ids.isdisjoint(final_ids)
        assert second_ids.isdisjoint(final_ids)

    @pytest.mark.asyncio
    async def test_list_deliveries_returns_empty_list_when_tenant_has_none(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self.create_delivery(
            session_factory,
            tenant_slug="tenant-b",
        )

        response = await client.get(
            self.base_url,
            headers=self.auth("a"),
        )

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("params", "expected_status"),
        [
            ({"limit": 0}, 422),
            ({"limit": 101}, 422),
            ({"offset": -1}, 422),
        ],
    )
    async def test_list_deliveries_rejects_invalid_pagination(
        self,
        client: httpx.AsyncClient,
        params: dict[str, int],
        expected_status: int,
    ) -> None:
        response = await client.get(
            self.base_url,
            params=params,
            headers=self.auth("a"),
        )

        assert response.status_code == expected_status


class TestDeliveryDetailAPI(DeliveryAPITestHelpers):
    @pytest.mark.asyncio
    async def test_get_delivery_returns_attempt_history_in_order(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        delivery_id = await self.create_delivery(
            session_factory,
            status="failed",
            attempts=3,
        )

        response = await client.get(
            f"{self.base_url}/{delivery_id}",
            headers=self.auth("a"),
        )

        assert response.status_code == 200

        body = response.json()

        assert body["id"] == str(delivery_id)
        assert body["status"] == "failed"
        assert body["attempts"] == 3

        history = body["attempt_history"]

        assert len(history) == 3
        assert [attempt["attempt_number"] for attempt in history] == [1, 2, 3]

        assert all(attempt["outcome"] == "failed" for attempt in history)
        assert all(attempt["status_code"] == 500 for attempt in history)

    @pytest.mark.asyncio
    async def test_get_delivery_returns_empty_attempt_history(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        delivery_id = await self.create_delivery(
            session_factory,
            status="pending",
            attempts=0,
        )

        response = await client.get(
            f"{self.base_url}/{delivery_id}",
            headers=self.auth("a"),
        )

        assert response.status_code == 200

        body = response.json()
        assert body["id"] == str(delivery_id)
        assert body["status"] == "pending"
        assert body["attempts"] == 0
        assert body["attempt_history"] == []

    @pytest.mark.asyncio
    async def test_get_another_tenants_delivery_returns_404(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant_b_delivery_id = await self.create_delivery(
            session_factory,
            tenant_slug="tenant-b",
        )

        response = await client.get(
            f"{self.base_url}/{tenant_b_delivery_id}",
            headers=self.auth("a"),
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_get_nonexistent_delivery_returns_404(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        nonexistent_id = uuid.uuid4()

        response = await client.get(
            f"{self.base_url}/{nonexistent_id}",
            headers=self.auth("a"),
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_get_delivery_rejects_invalid_uuid(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        response = await client.get(
            f"{self.base_url}/not-a-valid-uuid",
            headers=self.auth("a"),
        )

        assert response.status_code == 422
