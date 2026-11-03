# tests/integration/test_delivery_replay.py

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
        return f"sqlite+aiosqlite:///{tmp_path / 'delivery-replay.db'}"

    url = os.environ.get("HOOKRELAY_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("HOOKRELAY_TEST_POSTGRES_URL is not configured.")

    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("HOOKRELAY_TEST_POSTGRES_URL must use 'postgresql+asyncpg://'.")

    return url


@pytest_asyncio.fixture
async def session_factory(database_url):
    # IMPORTANT: this fixture drops and recreates tables.
    # Use only a disposable test database, never a development or production DB.
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


class DeliveryReplayHelpers:
    base_url = "/api/v1/deliveries"

    @staticmethod
    def auth(tenant: str = "a") -> dict[str, str]:
        return {"X-API-Key": f"tenant-{tenant}-test-key"}

    @staticmethod
    async def tenant_id(
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
        attempts: int | None = None,
    ) -> uuid.UUID:
        if attempts is None:
            attempts = 2 if status == "failed" else (1 if status == "succeeded" else 0)

        if attempts < 0:
            raise ValueError("attempts cannot be negative.")

        tenant_id = await cls.tenant_id(session_factory, tenant_slug)
        now = datetime.now(UTC)

        async with session_factory.begin() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant_id,
                url="https://receiver.example.test/webhook",
                description="Delivery replay integration test",
                is_active=True,
            )
            event = Event(
                tenant_id=tenant_id,
                event_type="invoice.created",
                payload={"invoice_id": "inv-replay-test"},
                idempotency_key=f"replay-test-{uuid.uuid4()}",
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
            )
            session.add(delivery)
            await session.flush()

            # Seed realistic attempt history for completed deliveries.
            for attempt_number in range(1, attempts + 1):
                is_last_success = status == "succeeded" and attempt_number == attempts
                started_at = now - timedelta(minutes=attempt_number)

                session.add(
                    DeliveryAttempt(
                        delivery_id=delivery.id,
                        attempt_number=attempt_number,
                        status_code=200 if is_last_success else 500,
                        error_message=(
                            None
                            if is_last_success
                            else f"Test failure {attempt_number}"
                        ),
                        outcome="succeeded" if is_last_success else "failed",
                        started_at=started_at,
                        finished_at=started_at + timedelta(seconds=2),
                    )
                )

            await session.flush()
            return delivery.id

    @staticmethod
    async def get_delivery(
        session_factory: async_sessionmaker[AsyncSession],
        delivery_id: uuid.UUID,
    ) -> Delivery:
        async with session_factory() as session:
            result = await session.execute(
                select(Delivery).where(Delivery.id == delivery_id)
            )
            delivery = result.scalar_one_or_none()

            if delivery is None:
                raise AssertionError(f"Delivery {delivery_id} was not found.")

            return delivery

    @staticmethod
    async def get_attempts(
        session_factory: async_sessionmaker[AsyncSession],
        delivery_id: uuid.UUID,
    ) -> list[DeliveryAttempt]:
        async with session_factory() as session:
            result = await session.execute(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_number.asc())
            )
            return list(result.scalars().all())


class TestDeliveryReplay(DeliveryReplayHelpers):
    @pytest.mark.asyncio
    async def test_replaying_failed_delivery_creates_pending_delivery(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        original_id = await self.create_delivery(
            session_factory,
            status="failed",
            attempts=2,
        )

        original_before = await self.get_delivery(
            session_factory,
            original_id,
        )
        original_attempts_before = await self.get_attempts(
            session_factory,
            original_id,
        )

        response = await client.post(
            f"{self.base_url}/{original_id}/replay",
            headers=self.auth("a"),
        )

        assert response.status_code == 201

        body = response.json()
        replay_id = uuid.UUID(body["id"])

        assert replay_id != original_id
        assert body["event_id"] == str(original_before.event_id)
        assert body["endpoint_id"] == str(original_before.endpoint_id)
        assert body["replay_of_delivery_id"] == str(original_id)
        assert body["status"] == "pending"
        assert body["attempts"] == 0

        replay = await self.get_delivery(session_factory, replay_id)
        assert replay.event_id == original_before.event_id
        assert replay.endpoint_id == original_before.endpoint_id
        assert replay.replay_of_delivery_id == original_id
        assert replay.status == "pending"
        assert replay.attempts == 0
        assert replay.locked_until is None
        assert replay.lease_token is None

        # A new replay has no attempt history until a worker claims it.
        replay_attempts = await self.get_attempts(
            session_factory,
            replay_id,
        )
        assert replay_attempts == []

        # Replaying must not alter the original delivery.
        original_after = await self.get_delivery(
            session_factory,
            original_id,
        )
        original_attempts_after = await self.get_attempts(
            session_factory,
            original_id,
        )

        assert original_after.status == "failed"
        assert original_after.attempts == original_before.attempts
        assert original_after.event_id == original_before.event_id
        assert original_after.endpoint_id == original_before.endpoint_id

        assert len(original_attempts_after) == len(original_attempts_before)
        assert [
            (
                attempt.attempt_number,
                attempt.status_code,
                attempt.error_message,
                attempt.outcome,
                attempt.started_at,
                attempt.finished_at,
            )
            for attempt in original_attempts_after
        ] == [
            (
                attempt.attempt_number,
                attempt.status_code,
                attempt.error_message,
                attempt.outcome,
                attempt.started_at,
                attempt.finished_at,
            )
            for attempt in original_attempts_before
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["pending", "processing", "succeeded"])
    async def test_non_failed_delivery_cannot_be_replayed(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        status: str,
    ) -> None:
        attempts = 1 if status in {"processing", "succeeded"} else 0

        delivery_id = await self.create_delivery(
            session_factory,
            status=status,
            attempts=attempts,
        )

        response = await client.post(
            f"{self.base_url}/{delivery_id}/replay",
            headers=self.auth("a"),
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "invalid_delivery_state"

        # No replay should have been persisted.
        async with session_factory() as session:
            result = await session.execute(
                select(Delivery).where(Delivery.replay_of_delivery_id == delivery_id)
            )
            replays = list(result.scalars().all())

        assert replays == []

        original = await self.get_delivery(session_factory, delivery_id)
        assert original.status == status
        assert original.attempts == attempts

    @pytest.mark.asyncio
    async def test_replaying_nonexistent_delivery_returns_not_found(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        nonexistent_id = uuid.uuid4()

        response = await client.post(
            f"{self.base_url}/{nonexistent_id}/replay",
            headers=self.auth("a"),
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_repeated_replay_creates_distinct_deliveries(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        original_id = await self.create_delivery(
            session_factory,
            status="failed",
            attempts=2,
        )

        first_response = await client.post(
            f"{self.base_url}/{original_id}/replay",
            headers=self.auth("a"),
        )
        second_response = await client.post(
            f"{self.base_url}/{original_id}/replay",
            headers=self.auth("a"),
        )

        assert first_response.status_code == 201
        assert second_response.status_code == 201

        first_replay = first_response.json()
        second_replay = second_response.json()

        first_replay_id = uuid.UUID(first_replay["id"])
        second_replay_id = uuid.UUID(second_replay["id"])

        assert first_replay_id != second_replay_id
        assert first_replay["replay_of_delivery_id"] == str(original_id)
        assert second_replay["replay_of_delivery_id"] == str(original_id)
        assert first_replay["status"] == "pending"
        assert second_replay["status"] == "pending"

        async with session_factory() as session:
            result = await session.execute(
                select(Delivery).where(Delivery.replay_of_delivery_id == original_id)
            )
            replays = list(result.scalars().all())

        assert len(replays) == 2
        assert {replay.id for replay in replays} == {
            first_replay_id,
            second_replay_id,
        }
