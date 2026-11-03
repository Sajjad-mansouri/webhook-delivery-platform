# tests/integration/test_event_idempotency.py

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
    """Provide an isolated SQLite database or a dedicated PostgreSQL test DB."""
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'idempotency-tests.db'}"

    url = os.environ.get("HOOKRELAY_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("HOOKRELAY_TEST_POSTGRES_URL is not configured.")

    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail(
            "HOOKRELAY_TEST_POSTGRES_URL must use the 'postgresql+asyncpg://' driver."
        )

    return url


@pytest_asyncio.fixture
async def session_factory(database_url):
    """Create the schema and seed two tenants for one test."""
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
    """Use the test database and map test API keys to tenants."""

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def override_get_current_tenant(
        db: Annotated[AsyncSession, Depends(get_db)],
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> Tenant:
        tenant_slugs = {
            "tenant-a-test-key": "tenant-a",
            "tenant-b-test-key": "tenant-b",
        }

        tenant_slug = tenant_slugs.get(x_api_key or "")
        if tenant_slug is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid API key.",
            )

        result = await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
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


class TestEventIdempotency:
    base_url = "/api/v1/events"

    @staticmethod
    def auth(tenant: str = "a") -> dict[str, str]:
        return {"X-API-Key": f"tenant-{tenant}-test-key"}

    @staticmethod
    async def create_matching_subscription(session_factory) -> None:
        """Subscribe Tenant A to the event type used by these tests."""
        async with session_factory.begin() as session:
            result = await session.execute(
                select(Tenant).where(Tenant.slug == "tenant-a")
            )
            tenant = result.scalar_one()

            endpoint = WebhookEndpoint(
                tenant_id=tenant.id,
                url="https://receiver.example.test/webhook",
                description="Test webhook endpoint",
                is_active=True,
            )
            session.add(endpoint)
            await session.flush()

            session.add(
                EndpointSubscription(
                    endpoint_id=endpoint.id,
                    event_type="invoice.created",
                )
            )

    @staticmethod
    async def get_events_by_key(
        session_factory,
        idempotency_key: str,
    ) -> list[Event]:
        async with session_factory() as session:
            result = await session.execute(
                select(Event).where(Event.idempotency_key == idempotency_key)
            )
            return list(result.scalars().all())

    @pytest.mark.asyncio
    async def test_repeating_same_key_and_payload_returns_original_event(
        self,
        client: httpx.AsyncClient,
        session_factory,
    ) -> None:
        await self.create_matching_subscription(session_factory)

        idempotency_key = "invoice-created-retry-001"
        payload = {
            "event_type": "invoice.created",
            "payload": {
                "invoice_id": "inv-123",
                "amount": 2500,
            },
        }
        headers = {
            **self.auth("a"),
            "Idempotency-Key": idempotency_key,
        }

        first_response = await client.post(
            self.base_url,
            headers=headers,
            json=payload,
        )
        second_response = await client.post(
            self.base_url,
            headers=headers,
            json=payload,
        )

        assert first_response.status_code == 201
        assert second_response.status_code == 200

        first_event_id = uuid.UUID(first_response.json()["id"])
        second_event_id = uuid.UUID(second_response.json()["id"])

        assert first_event_id == second_event_id

        events = await self.get_events_by_key(
            session_factory,
            idempotency_key,
        )
        assert len(events) == 1
        assert events[0].id == first_event_id

        async with session_factory() as session:
            result = await session.execute(
                select(Delivery).where(Delivery.event_id == first_event_id)
            )
            deliveries = list(result.scalars().all())

        # The duplicate request must not create a second delivery.
        assert len(deliveries) == 1
        assert deliveries[0].status == "pending"
        assert deliveries[0].attempts == 0

    @pytest.mark.asyncio
    async def test_reusing_key_with_different_payload_returns_conflict(
        self,
        client: httpx.AsyncClient,
        session_factory,
    ) -> None:
        await self.create_matching_subscription(session_factory)

        idempotency_key = "invoice-created-conflict-001"
        headers = {
            **self.auth("a"),
            "Idempotency-Key": idempotency_key,
        }

        first_response = await client.post(
            self.base_url,
            headers=headers,
            json={
                "event_type": "invoice.created",
                "payload": {"invoice_id": "inv-123", "amount": 2500},
            },
        )
        assert first_response.status_code == 201

        conflicting_response = await client.post(
            self.base_url,
            headers=headers,
            json={
                "event_type": "invoice.created",
                "payload": {"invoice_id": "inv-123", "amount": 9999},
            },
        )

        assert conflicting_response.status_code == 409
        assert (
            conflicting_response.json()["error"]["code"] == "idempotency_key_conflict"
        )

        events = await self.get_events_by_key(
            session_factory,
            idempotency_key,
        )
        assert len(events) == 1
        assert events[0].payload == {
            "invoice_id": "inv-123",
            "amount": 2500,
        }

        async with session_factory() as session:
            result = await session.execute(
                select(Delivery).where(Delivery.event_id == events[0].id)
            )
            deliveries = list(result.scalars().all())

        assert len(deliveries) == 1

    @pytest.mark.asyncio
    async def test_same_key_is_independent_between_tenants(
        self,
        client: httpx.AsyncClient,
        session_factory,
    ) -> None:
        idempotency_key = "tenant-scoped-key-001"
        payload = {
            "event_type": "invoice.created",
            "payload": {"invoice_id": "inv-shared", "amount": 2500},
        }

        tenant_a_response = await client.post(
            self.base_url,
            headers={
                **self.auth("a"),
                "Idempotency-Key": idempotency_key,
            },
            json=payload,
        )
        tenant_b_response = await client.post(
            self.base_url,
            headers={
                **self.auth("b"),
                "Idempotency-Key": idempotency_key,
            },
            json=payload,
        )

        assert tenant_a_response.status_code == 201
        assert tenant_b_response.status_code == 201

        tenant_a_event_id = uuid.UUID(tenant_a_response.json()["id"])
        tenant_b_event_id = uuid.UUID(tenant_b_response.json()["id"])

        assert tenant_a_event_id != tenant_b_event_id

        events = await self.get_events_by_key(
            session_factory,
            idempotency_key,
        )
        assert len(events) == 2
        assert {event.id for event in events} == {
            tenant_a_event_id,
            tenant_b_event_id,
        }

        async with session_factory() as session:
            tenant_a_result = await session.execute(
                select(Tenant).where(Tenant.slug == "tenant-a")
            )
            tenant_a = tenant_a_result.scalar_one()

            tenant_b_result = await session.execute(
                select(Tenant).where(Tenant.slug == "tenant-b")
            )
            tenant_b = tenant_b_result.scalar_one()

        assert {event.id: event.tenant_id for event in events} == {
            tenant_a_event_id: tenant_a.id,
            tenant_b_event_id: tenant_b.id,
        }
