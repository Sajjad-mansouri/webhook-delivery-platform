# tests/test_deliveries_api.py

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory():
    """Create an isolated in-memory SQLite database for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async with factory() as session:
        session.add_all(
            [
                Tenant(name="Tenant A", slug="tenant-a"),
                Tenant(name="Tenant B", slug="tenant-b"),
            ]
        )
        await session.commit()

    yield factory

    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_factory):
    """Create an HTTP client with isolated database and auth dependencies."""

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


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class DeliveryTestHelpers:
    """Reusable helpers for delivery history and replay tests."""

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
        """Create an event, endpoint, delivery, and its attempt history."""
        tenant_id = await cls.get_tenant_id(
            session_factory,
            tenant_slug,
        )

        now = datetime.now(UTC)

        async with session_factory() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant_id,
                url="https://receiver.example.test/webhook",
                description="Test webhook endpoint",
                is_active=True,
            )
            event = Event(
                tenant_id=tenant_id,
                event_type="order.created",
                payload={"order_id": 123},
                idempotency_key=f"test-{uuid.uuid4()}",
            )

            session.add_all([endpoint, event])
            await session.flush()

            delivery = Delivery(
                event_id=event.id,
                endpoint_id=endpoint.id,
                status=status,
                attempts=attempts,
                available_at=now,
                locked_until=(
                    now + timedelta(seconds=60) if status == "processing" else None
                ),
                lease_token=(uuid.uuid4() if status == "processing" else None),
            )

            session.add(delivery)
            await session.flush()

            # Insert in reverse order so the API test can verify that the
            # response sorts attempts by attempt_number, not insertion order.
            for attempt_number in reversed(range(1, attempts + 1)):
                is_current_processing_attempt = (
                    status == "processing" and attempt_number == attempts
                )
                is_successful_attempt = (
                    status == "succeeded" and attempt_number == attempts
                )

                outcome = (
                    "processing"
                    if is_current_processing_attempt
                    else "succeeded"
                    if is_successful_attempt
                    else "failed"
                )

                started_at = now - timedelta(minutes=attempt_number)
                finished_at = (
                    None
                    if is_current_processing_attempt
                    else started_at + timedelta(seconds=2)
                )

                session.add(
                    DeliveryAttempt(
                        delivery_id=delivery.id,
                        attempt_number=attempt_number,
                        status_code=(
                            None
                            if is_current_processing_attempt
                            else 200
                            if is_successful_attempt
                            else 500
                        ),
                        error_message=(
                            None
                            if is_current_processing_attempt or is_successful_attempt
                            else f"Test failure {attempt_number}"
                        ),
                        outcome=outcome,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                )

            await session.commit()
            return delivery.id

    @classmethod
    async def get_delivery(
        cls,
        client: httpx.AsyncClient,
        delivery_id: uuid.UUID,
        *,
        tenant: str = "a",
    ) -> httpx.Response:
        return await client.get(
            f"{cls.base_url}/{delivery_id}",
            headers=cls.auth(tenant),
        )

    @classmethod
    async def get_attempts(
        cls,
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


# ---------------------------------------------------------------------------
# Delivery history API
# ---------------------------------------------------------------------------


class TestDeliveryHistoryAPI(DeliveryTestHelpers):
    @pytest.mark.asyncio
    async def test_list_deliveries_is_tenant_scoped(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        tenant_a_delivery_id = await self.create_delivery(
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

        deliveries = response.json()
        delivery_ids = {item["id"] for item in deliveries}

        assert str(tenant_a_delivery_id) in delivery_ids
        assert len(deliveries) == 1

    @pytest.mark.asyncio
    async def test_get_delivery_includes_attempt_history(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        delivery_id = await self.create_delivery(
            session_factory,
            status="failed",
            attempts=3,
        )

        response = await self.get_delivery(client, delivery_id)

        assert response.status_code == 200

        body = response.json()
        assert body["id"] == str(delivery_id)
        assert body["attempts"] == 3
        assert body["status"] == "failed"

        history = body["attempt_history"]
        assert len(history) == 3
        assert [attempt["attempt_number"] for attempt in history] == [1, 2, 3]

        assert all(attempt["outcome"] == "failed" for attempt in history)

    @pytest.mark.asyncio
    async def test_cross_tenant_delivery_returns_404(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        delivery_id = await self.create_delivery(
            session_factory,
            tenant_slug="tenant-b",
        )

        detail_response = await self.get_delivery(
            client,
            delivery_id,
            tenant="a",
        )

        replay_response = await client.post(
            f"{self.base_url}/{delivery_id}/replay",
            headers=self.auth("a"),
        )

        assert detail_response.status_code == 404
        assert replay_response.status_code == 404


# ---------------------------------------------------------------------------
# Manual replay API
# ---------------------------------------------------------------------------


class TestDeliveryReplayAPI(DeliveryTestHelpers):
    @pytest.mark.asyncio
    async def test_replay_creates_new_pending_delivery(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        original_id = await self.create_delivery(
            session_factory,
            status="failed",
            attempts=2,
        )

        response = await client.post(
            f"{self.base_url}/{original_id}/replay",
            headers=self.auth("a"),
        )

        assert response.status_code == 201

        replay = response.json()
        assert replay["id"] != str(original_id)
        assert replay["event_id"]
        assert replay["endpoint_id"]
        assert replay["replay_of_delivery_id"] == str(original_id)
        assert replay["status"] == "pending"
        assert replay["attempts"] == 0

        replay_id = uuid.UUID(replay["id"])
        replay_attempts = await self.get_attempts(
            session_factory,
            replay_id,
        )

        assert replay_attempts == []

        original_response = await self.get_delivery(
            client,
            original_id,
        )
        assert original_response.status_code == 200
        assert original_response.json()["status"] == "failed"

    @pytest.mark.asyncio
    async def test_replay_preserves_original_attempt_history(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        original_id = await self.create_delivery(
            session_factory,
            status="failed",
            attempts=3,
        )

        before_response = await self.get_delivery(
            client,
            original_id,
        )
        assert before_response.status_code == 200
        before = before_response.json()

        replay_response = await client.post(
            f"{self.base_url}/{original_id}/replay",
            headers=self.auth("a"),
        )
        assert replay_response.status_code == 201

        after_response = await self.get_delivery(
            client,
            original_id,
        )
        assert after_response.status_code == 200
        after = after_response.json()

        assert after["id"] == before["id"]
        assert after["status"] == before["status"]
        assert after["attempts"] == before["attempts"]
        assert after["attempt_history"] == before["attempt_history"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        ["pending", "processing", "succeeded"],
    )
    async def test_replay_of_non_failed_delivery_returns_409(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        status: str,
    ) -> None:
        delivery_id = await self.create_delivery(
            session_factory,
            status=status,
            attempts=1,
        )

        response = await client.post(
            f"{self.base_url}/{delivery_id}/replay",
            headers=self.auth("a"),
        )

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_replay_of_replay_preserves_parent_link(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        original_id = await self.create_delivery(
            session_factory,
            status="failed",
            attempts=1,
        )

        first_response = await client.post(
            f"{self.base_url}/{original_id}/replay",
            headers=self.auth("a"),
        )
        assert first_response.status_code == 201

        first_replay_id = uuid.UUID(first_response.json()["id"])

        # A replay starts pending. Transition it to failed to model a replay
        # that was actually attempted and then exhausted its retry budget.
        async with session_factory() as session:
            result = await session.execute(
                select(Delivery).where(Delivery.id == first_replay_id)
            )
            first_replay = result.scalar_one()

            first_replay.status = "failed"
            first_replay.attempts = 1
            first_replay.locked_until = None
            first_replay.lease_token = None

            now = datetime.now(UTC)
            session.add(
                DeliveryAttempt(
                    delivery_id=first_replay_id,
                    attempt_number=1,
                    status_code=500,
                    error_message="Simulated failed replay",
                    outcome="failed",
                    started_at=now - timedelta(seconds=2),
                    finished_at=now,
                )
            )
            await session.commit()

        second_response = await client.post(
            f"{self.base_url}/{first_replay_id}/replay",
            headers=self.auth("a"),
        )

        assert second_response.status_code == 201

        second_replay = second_response.json()
        assert second_replay["id"] != str(first_replay_id)
        assert second_replay["replay_of_delivery_id"] == str(first_replay_id)

        # The second replay points to its immediate parent, not the root.
        assert second_replay["replay_of_delivery_id"] != str(original_id)

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

        assert first_replay["id"] != second_replay["id"]
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
        assert len({replay.id for replay in replays}) == 2
