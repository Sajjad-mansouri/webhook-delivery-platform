import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from app import models  # noqa: F401
from app.db.base import Base
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.event import Event
from app.models.tenant import Tenant
from app.models.webhook_endpoint import WebhookEndpoint
from app.security.webhook_signatures import encrypt_signing_secret
from app.worker.delivery import execute_delivery
from app.worker.queue import (
    claim_deliveries,
    recover_expired_deliveries,
)
from app.worker.retry import DEFAULT_MAX_ATTEMPTS
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture(params=["sqlite", "postgres"], ids=["sqlite", "postgres"])
def database_url(request, tmp_path) -> str:
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'worker-tests.db'}"

    url = os.environ.get("HOOKRELAY_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("HOOKRELAY_TEST_POSTGRES_URL is not configured.")

    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("HOOKRELAY_TEST_POSTGRES_URL must use 'postgresql+asyncpg://'.")

    return url


@pytest_asyncio.fixture
async def session_factory(database_url):
    # WARNING: This fixture drops and recreates every table.
    # Use only an isolated, disposable test database.
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
            session.add(Tenant(name="Worker Test Tenant", slug="worker-test"))

        yield factory
    finally:
        await engine.dispose()


class WorkerTestHelpers:
    @staticmethod
    def utc(value: datetime) -> datetime:
        # SQLite may return naive datetimes for timezone-aware columns.
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def create_delivery(
        self,
        session_factory,
        *,
        status: str = "pending",
        attempts: int = 0,
        available_at: datetime | None = None,
        locked_until: datetime | None = None,
        lease_token: uuid.UUID | None = None,
        endpoint_secret: str | None = "test-signing-secret",
    ) -> tuple[uuid.UUID, uuid.UUID | None]:
        """Create a delivery and consistent attempt history."""
        delivery_id = uuid.uuid4()
        now = datetime.now(UTC)

        if status == "processing":
            if attempts < 1:
                raise ValueError("A processing delivery requires at least one attempt.")
            if lease_token is None:
                lease_token = uuid.uuid4()
            if locked_until is None:
                locked_until = now + timedelta(minutes=1)
        else:
            lease_token = None
            locked_until = None

        encrypted_secret = (
            encrypt_signing_secret(endpoint_secret)
            if endpoint_secret is not None
            else None
        )

        async with session_factory.begin() as session:
            tenant = Tenant(
                name="Worker Test Tenant",
                slug=f"worker-{uuid.uuid4().hex[:12]}",
            )
            session.add(tenant)
            await session.flush()

            endpoint = WebhookEndpoint(
                tenant_id=tenant.id,
                url="https://receiver.example.test/webhook",
                description="Worker integration test",
                is_active=True,
                signing_secret_encrypted=encrypted_secret,
            )

            event = Event(
                tenant_id=tenant.id,
                event_type="order.created",
                payload={"order_id": str(uuid.uuid4())},
                idempotency_key=f"worker-{uuid.uuid4()}",
            )

            session.add_all([endpoint, event])
            await session.flush()

            delivery = Delivery(
                id=delivery_id,
                event_id=event.id,
                endpoint_id=endpoint.id,
                status=status,
                attempts=attempts,
                available_at=available_at or now,
                locked_until=locked_until,
                lease_token=lease_token,
                created_at=now,
            )
            session.add(delivery)

            # Earlier attempts have already failed.
            for number in range(1, attempts):
                session.add(
                    DeliveryAttempt(
                        delivery_id=delivery_id,
                        attempt_number=number,
                        outcome="failed",
                        error_message="Previous attempt failed.",
                        started_at=now - timedelta(minutes=number),
                        finished_at=now - timedelta(minutes=number),
                    )
                )

            if status == "processing":
                session.add(
                    DeliveryAttempt(
                        delivery_id=delivery_id,
                        attempt_number=attempts,
                        outcome="processing",
                        started_at=now,
                    )
                )

        return delivery_id, lease_token

    async def load_delivery(self, session_factory, delivery_id):
        async with session_factory() as session:
            return await session.get(Delivery, delivery_id)

    async def load_attempts(self, session_factory, delivery_id):
        async with session_factory() as session:
            result = await session.execute(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_number)
            )
            return list(result.scalars().all())


class TestClaimDeliveries(WorkerTestHelpers):
    @pytest.mark.asyncio
    async def test_claim_changes_state_and_creates_attempt(self, session_factory):
        delivery_id, _ = await self.create_delivery(session_factory)

        claimed = await claim_deliveries(
            session_factory,
            batch_size=10,
            lease_seconds=30,
        )

        assert len(claimed) == 1
        assert claimed[0].id == delivery_id
        assert claimed[0].status == "processing"
        assert claimed[0].attempts == 1
        assert claimed[0].lease_token is not None
        assert claimed[0].locked_until is not None

        attempts = await self.load_attempts(session_factory, delivery_id)
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1
        assert attempts[0].outcome == "processing"
        assert attempts[0].finished_at is None

    @pytest.mark.asyncio
    async def test_claim_skips_future_and_exhausted_deliveries(self, session_factory):
        now = datetime.now(UTC)

        future_id, _ = await self.create_delivery(
            session_factory,
            available_at=now + timedelta(hours=1),
        )

        exhausted_id, _ = await self.create_delivery(
            session_factory,
            status="pending",
            attempts=0,
            available_at=now - timedelta(seconds=1),
        )

        async with session_factory() as session:
            exhausted = await session.get(Delivery, exhausted_id)
            assert exhausted is not None
            exhausted.attempts = DEFAULT_MAX_ATTEMPTS

            for attempt_number in range(1, DEFAULT_MAX_ATTEMPTS + 1):
                session.add(
                    DeliveryAttempt(
                        delivery_id=exhausted_id,
                        attempt_number=attempt_number,
                        outcome="failed",
                        started_at=now - timedelta(seconds=attempt_number),
                        finished_at=now - timedelta(seconds=attempt_number),
                    )
                )

            await session.commit()

        claimed = await claim_deliveries(
            session_factory,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )

        claimed_ids = {item.id for item in claimed}

        assert future_id not in claimed_ids
        assert exhausted_id not in claimed_ids


class TestExecuteDelivery(WorkerTestHelpers):
    @pytest.mark.asyncio
    async def test_successful_response_finalizes_delivery_and_attempt(
        self, session_factory
    ):
        delivery_id, lease_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
        )
        assert lease_token is not None
        received = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            received["method"] = request.method
            received["headers"] = dict(request.headers)
            received["body"] = json.loads(request.content)
            return httpx.Response(204)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await execute_delivery(session_factory, client, delivery_id, lease_token)

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert received["method"] == "POST"
        assert received["headers"]["content-type"].startswith("application/json")
        assert "x-hookrelay-event-id" in received["headers"]
        assert "x-hookplatfom-signature" in received["headers"]
        assert "x-hookplatfom-timestamp" in received["headers"]
        assert received["body"]["event_type"] == "order.created"
        assert "order_id" in received["body"]["payload"]

        assert delivery is not None
        assert delivery.status == "succeeded"
        assert delivery.attempts == 1
        assert delivery.lease_token is None
        assert delivery.locked_until is None

        assert len(attempts) == 1
        assert attempts[0].outcome == "succeeded"
        assert attempts[0].status_code == 204
        assert attempts[0].error_message is None
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "expected_status"),
        [
            (200, "succeeded"),
            (204, "succeeded"),
            (400, "failed"),
            (401, "failed"),
            (404, "failed"),
            (408, "pending"),
            (429, "pending"),
            (500, "pending"),
            (503, "pending"),
        ],
    )
    async def test_http_status_classification(
        self, session_factory, status_code, expected_status
    ):
        delivery_id, lease_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
        )
        assert lease_token is not None

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await execute_delivery(session_factory, client, delivery_id, lease_token)

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == expected_status
        assert delivery.lease_token is None
        assert delivery.locked_until is None

        assert len(attempts) == 1
        assert attempts[0].status_code == status_code
        assert attempts[0].finished_at is not None

        if expected_status == "succeeded":
            assert attempts[0].outcome == "succeeded"
            assert attempts[0].error_message is None
        else:
            assert attempts[0].outcome == "failed"
            assert attempts[0].error_message == (
                f"Destination returned HTTP {status_code}."
            )

        if expected_status == "pending":
            assert self.utc(delivery.available_at) > datetime.now(UTC)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exception_type", "expected_message"),
        [
            (httpx.ConnectError, "Network request failed."),
            (httpx.ReadTimeout, "Request timed out."),
        ],
    )
    async def test_network_failure_schedules_retry(
        self,
        session_factory,
        exception_type,
        expected_message,
    ):
        delivery_id, lease_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
        )
        assert lease_token is not None

        async def handler(request: httpx.Request) -> httpx.Response:
            raise exception_type("simulated failure", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await execute_delivery(session_factory, client, delivery_id, lease_token)

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.lease_token is None
        assert delivery.locked_until is None
        assert self.utc(delivery.available_at) > datetime.now(UTC)

        assert len(attempts) == 1
        assert attempts[0].status_code is None
        assert attempts[0].outcome == "failed"
        assert attempts[0].error_message == expected_message
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_retryable_response_at_attempt_limit_fails(self, session_factory):
        delivery_id, lease_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=2,
        )
        assert lease_token is not None

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                lease_token,
                max_attempts=2,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.attempts == 2
        assert delivery.lease_token is None
        assert len(attempts) == 2
        assert attempts[-1].attempt_number == 2
        assert attempts[-1].status_code == 503
        assert attempts[-1].outcome == "failed"

    @pytest.mark.asyncio
    async def test_stale_token_does_not_send_request(self, session_factory):
        delivery_id, current_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
        )
        assert current_token is not None
        request_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(204)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                uuid.uuid4(),
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert request_count == 0
        assert delivery is not None
        assert delivery.status == "processing"
        assert delivery.lease_token == current_token
        assert len(attempts) == 1
        assert attempts[0].outcome == "processing"

    @pytest.mark.asyncio
    async def test_lease_replaced_during_request_cannot_be_overwritten(
        self, session_factory
    ):
        delivery_id, old_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
        )
        assert old_token is not None
        new_token = uuid.uuid4()

        async def handler(request: httpx.Request) -> httpx.Response:
            # Simulate another worker taking ownership while this
            # worker's HTTP request is in progress.
            async with session_factory.begin() as session:
                delivery = await session.get(Delivery, delivery_id)
                assert delivery is not None
                delivery.status = "processing"
                delivery.lease_token = new_token
                delivery.locked_until = datetime.now(UTC) + timedelta(minutes=1)

            return httpx.Response(204)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await execute_delivery(session_factory, client, delivery_id, old_token)

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "processing"
        assert delivery.lease_token == new_token

        # The old worker must not finalize the attempt.
        assert len(attempts) == 1
        assert attempts[0].outcome == "processing"
        assert attempts[0].finished_at is None


class TestRecoverExpiredDeliveries(WorkerTestHelpers):
    @pytest.mark.asyncio
    async def test_expired_lease_is_abandoned_and_scheduled_for_retry(
        self, session_factory
    ):
        delivery_id, _ = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
            locked_until=datetime.now(UTC) - timedelta(seconds=5),
        )

        recovered = await recover_expired_deliveries(
            session_factory,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert recovered == 1
        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.lease_token is None
        assert delivery.locked_until is None
        assert self.utc(delivery.available_at) > datetime.now(UTC)

        assert len(attempts) == 1
        assert attempts[0].outcome == "abandoned"
        assert attempts[0].error_message == (
            "Worker lease expired before the attempt was finalized."
        )
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_expired_lease_at_attempt_limit_fails(self, session_factory):
        delivery_id, _ = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=2,
            locked_until=datetime.now(UTC) - timedelta(seconds=5),
        )

        recovered = await recover_expired_deliveries(
            session_factory,
            max_attempts=2,
        )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert recovered == 1
        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.attempts == 2
        assert delivery.lease_token is None
        assert delivery.locked_until is None

        assert len(attempts) == 2
        assert attempts[-1].attempt_number == 2
        assert attempts[-1].outcome == "abandoned"
        assert attempts[-1].finished_at is not None

    @pytest.mark.asyncio
    async def test_unexpired_lease_is_not_recovered(self, session_factory):
        delivery_id, lease_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
            locked_until=datetime.now(UTC) + timedelta(minutes=5),
        )

        recovered = await recover_expired_deliveries(session_factory)

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert recovered == 0
        assert delivery is not None
        assert delivery.status == "processing"
        assert delivery.lease_token == lease_token
        assert len(attempts) == 1
        assert attempts[0].outcome == "processing"
        assert attempts[0].finished_at is None
