# tests/integration/test_tenant_isolation.py

import os
import uuid
from typing import Annotated

import httpx
import pytest
import pytest_asyncio
from app import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.delivery import Delivery
from app.models.endpoint_subscription import EndpointSubscription
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
        return f"sqlite+aiosqlite:///{tmp_path / 'tenant-isolation.db'}"

    url = os.environ.get("HOOKRELAY_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("HOOKRELAY_TEST_POSTGRES_URL is not configured.")

    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("HOOKRELAY_TEST_POSTGRES_URL must use 'postgresql+asyncpg://'.")

    return url


@pytest_asyncio.fixture
async def session_factory(database_url):
    engine = create_async_engine(database_url)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
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


class TenantIsolationHelpers:
    @staticmethod
    def auth(tenant: str = "a") -> dict[str, str]:
        return {"X-API-Key": f"tenant-{tenant}-test-key"}

    @staticmethod
    async def tenant_id(session_factory, slug: str) -> uuid.UUID:
        async with session_factory() as session:
            result = await session.execute(select(Tenant.id).where(Tenant.slug == slug))
            return result.scalar_one()

    @classmethod
    async def create_delivery(
        cls,
        session_factory,
        *,
        tenant_slug: str = "tenant-b",
        status: str = "failed",
    ) -> uuid.UUID:
        tenant_id = await cls.tenant_id(session_factory, tenant_slug)

        async with session_factory.begin() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant_id,
                url="https://receiver.example.test/webhook",
                description="Isolation test endpoint",
                is_active=True,
            )
            event = Event(
                tenant_id=tenant_id,
                event_type="invoice.created",
                payload={"invoice_id": "inv-test"},
                idempotency_key=f"isolation-{uuid.uuid4()}",
            )

            session.add_all([endpoint, event])
            await session.flush()

            delivery = Delivery(
                event_id=event.id,
                endpoint_id=endpoint.id,
                status=status,
                attempts=1,
            )
            session.add(delivery)
            await session.flush()

            return delivery.id

    @classmethod
    async def create_endpoint(
        cls,
        session_factory,
        *,
        tenant_slug: str,
        is_active: bool = True,
        event_type: str | None = None,
    ) -> uuid.UUID:
        tenant_id = await cls.tenant_id(session_factory, tenant_slug)

        async with session_factory.begin() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant_id,
                url="https://receiver.example.test/webhook",
                description="Isolation test endpoint",
                is_active=is_active,
            )
            session.add(endpoint)
            await session.flush()

            if event_type is not None:
                session.add(
                    EndpointSubscription(
                        endpoint_id=endpoint.id,
                        event_type=event_type,
                    )
                )

            await session.flush()
            return endpoint.id


class TestTenantIsolation(TenantIsolationHelpers):
    @pytest.mark.asyncio
    async def test_tenant_cannot_read_another_tenants_delivery(
        self,
        client: httpx.AsyncClient,
        session_factory,
    ) -> None:
        delivery_id = await self.create_delivery(
            session_factory,
            tenant_slug="tenant-b",
        )

        response = await client.get(
            f"/api/v1/deliveries/{delivery_id}",
            headers=self.auth("a"),
        )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_tenant_cannot_replay_another_tenants_delivery(
        self,
        client: httpx.AsyncClient,
        session_factory,
    ) -> None:
        delivery_id = await self.create_delivery(
            session_factory,
            tenant_slug="tenant-b",
            status="failed",
        )

        response = await client.post(
            f"/api/v1/deliveries/{delivery_id}/replay",
            headers=self.auth("a"),
        )

        assert response.status_code == 404

        # Confirm that the unauthorized request did not create a replay.
        async with session_factory() as session:
            result = await session.execute(
                select(Delivery).where(
                    Delivery.id == delivery_id,
                )
            )
            original = result.scalar_one()

            replay_result = await session.execute(
                select(Delivery).where(
                    Delivery.replay_of_delivery_id == delivery_id,
                )
            )
            replays = list(replay_result.scalars().all())

        assert original.status == "failed"
        assert replays == []

    @pytest.mark.asyncio
    async def test_tenant_cannot_subscribe_to_another_tenants_endpoint(
        self,
        client: httpx.AsyncClient,
        session_factory,
    ) -> None:
        endpoint_id = await self.create_endpoint(
            session_factory,
            tenant_slug="tenant-b",
        )

        response = await client.post(
            f"/api/v1/endpoints/{endpoint_id}/subscriptions",
            headers=self.auth("a"),
            json={"event_type": "invoice.created"},
        )

        assert response.status_code == 404

        async with session_factory() as session:
            result = await session.execute(
                select(EndpointSubscription).where(
                    EndpointSubscription.endpoint_id == endpoint_id,
                )
            )
            subscriptions = list(result.scalars().all())

        assert subscriptions == []

    @pytest.mark.asyncio
    async def test_event_creates_deliveries_only_for_own_active_subscriptions(
        self,
        client: httpx.AsyncClient,
        session_factory,
    ) -> None:
        # Tenant A: matching active endpoint.
        tenant_a_endpoint = await self.create_endpoint(
            session_factory,
            tenant_slug="tenant-a",
            is_active=True,
            event_type="invoice.created",
        )

        # Tenant A: matching subscription, but inactive endpoint.
        await self.create_endpoint(
            session_factory,
            tenant_slug="tenant-a",
            is_active=False,
            event_type="invoice.created",
        )

        # Tenant B: active endpoint subscribed to the same event type.
        await self.create_endpoint(
            session_factory,
            tenant_slug="tenant-b",
            is_active=True,
            event_type="invoice.created",
        )

        response = await client.post(
            "/api/v1/events",
            headers={
                **self.auth("a"),
                "Idempotency-Key": "tenant-isolation-event-001",
            },
            json={
                "event_type": "invoice.created",
                "payload": {"invoice_id": "inv-isolation-001"},
            },
        )

        assert response.status_code == 201
        event_id = uuid.UUID(response.json()["id"])

        async with session_factory() as session:
            event_result = await session.execute(
                select(Event).where(Event.id == event_id)
            )
            event = event_result.scalar_one()

            delivery_result = await session.execute(
                select(Delivery).where(Delivery.event_id == event_id)
            )
            deliveries = list(delivery_result.scalars().all())

        tenant_a_id = await self.tenant_id(session_factory, "tenant-a")

        assert event.tenant_id == tenant_a_id
        assert len(deliveries) == 1
        assert deliveries[0].endpoint_id == tenant_a_endpoint
        assert deliveries[0].status == "pending"
        assert deliveries[0].attempts == 0
