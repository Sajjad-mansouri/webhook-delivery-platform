import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from app.db.base import Base
from app.models import (
    Delivery,
    DeliveryAttempt,
    Event,
    Tenant,
    WebhookEndpoint,
)
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


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """Create an isolated database for each test."""
    database_path = tmp_path / "delivery-tests.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    try:
        yield factory
    finally:
        await engine.dispose()


class DeliveryTestHelpers:
    """Shared helpers for realistic delivery and attempt state."""

    async def create_delivery(
        self,
        session_factory,
        *,
        status: str = "processing",
        attempts: int = 1,
        locked_until: datetime | None = None,
        available_at: datetime | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID | None]:
        """
        Create a delivery with consistent attempt history.

        For a processing delivery, the current attempt must already
        exist and have outcome="processing", as it would after claiming.
        """
        if attempts < 0:
            raise ValueError("attempts cannot be negative.")

        if status == "processing":
            if attempts < 1:
                raise ValueError("A processing delivery must have attempts >= 1.")

            if lease_token is None:
                lease_token = uuid.uuid4()

            if locked_until is None:
                locked_until = datetime.now(UTC) + timedelta(seconds=60)
        else:
            lease_token = None
            locked_until = None

        tenant_id = uuid.uuid4()
        endpoint_id = uuid.uuid4()
        event_id = uuid.uuid4()
        delivery_id = uuid.uuid4()

        endpoint_url = "https://receiver.example/webhooks"
        now = datetime.now(UTC)

        # The worker requires an encrypted signing secret.
        # encrypt_signing_secret must use the same configured Fernet
        # key that the worker uses when decrypting this value.
        signing_secret_encrypted = encrypt_signing_secret("test-signing-secret")

        async with session_factory.begin() as session:
            tenant = Tenant(
                id=tenant_id,
                name="Test tenant",
                slug=f"tenant-{tenant_id.hex[:12]}",
            )

            endpoint = WebhookEndpoint(
                id=endpoint_id,
                tenant_id=tenant_id,
                url=endpoint_url,
                description="Test receiver",
                is_active=True,
                signing_secret_encrypted=signing_secret_encrypted,
            )

            event = Event(
                id=event_id,
                tenant_id=tenant_id,
                event_type="invoice.created",
                payload={
                    "invoice_id": "inv-123",
                    "amount": 2500,
                },
                idempotency_key=f"event-{event_id.hex}",
            )

            delivery = Delivery(
                id=delivery_id,
                event_id=event_id,
                endpoint_id=endpoint_id,
                status=status,
                attempts=attempts,
                available_at=available_at or now,
                locked_until=locked_until,
                lease_token=lease_token,
            )

            session.add_all([tenant, endpoint, event, delivery])

            # Create completed history for earlier attempts.
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

            # The currently claimed attempt is created before execution.
            if status == "processing":
                session.add(
                    DeliveryAttempt(
                        delivery_id=delivery_id,
                        attempt_number=attempts,
                        outcome="processing",
                        started_at=now,
                    )
                )

        return delivery_id, event_id, endpoint_url, lease_token

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

    @staticmethod
    def utc_datetime(value: datetime) -> datetime:
        """SQLite may return naive datetimes for timezone-aware columns."""
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)

        return value.astimezone(UTC)


class TestExecuteDelivery(DeliveryTestHelpers):
    @pytest.mark.asyncio
    async def test_successful_response_marks_delivery_succeeded(
        self,
        session_factory,
    ):
        delivery_id, event_id, endpoint_url, lease_token = await self.create_delivery(
            session_factory
        )
        received = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            received["method"] = request.method
            received["url"] = str(request.url)
            received["headers"] = dict(request.headers)
            received["body"] = json.loads(request.content)
            return httpx.Response(204)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                lease_token,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert received["method"] == "POST"
        assert received["url"] == endpoint_url
        assert received["headers"]["content-type"].startswith("application/json")
        assert received["headers"]["x-hookrelay-event-id"] == str(event_id)
        assert received["body"] == {
            "id": str(event_id),
            "event_type": "invoice.created",
            "payload": {
                "invoice_id": "inv-123",
                "amount": 2500,
            },
        }

        assert delivery is not None
        assert delivery.status == "succeeded"
        assert delivery.attempts == 1
        assert delivery.locked_until is None
        assert delivery.lease_token is None

        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1
        assert attempts[0].status_code == 204
        assert attempts[0].outcome == "succeeded"
        assert attempts[0].error_message is None
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status_code", "expected_status", "expected_outcome"),
        [
            (400, "failed", "failed"),
            (401, "failed", "failed"),
            (404, "failed", "failed"),
            (408, "pending", "failed"),
            (429, "pending", "failed"),
            (500, "pending", "failed"),
            (503, "pending", "failed"),
        ],
    )
    async def test_http_failure_is_classified_and_recorded(
        self,
        session_factory,
        status_code,
        expected_status,
        expected_outcome,
    ):
        delivery_id, _, _, lease_token = await self.create_delivery(session_factory)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, text="receiver error")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                lease_token,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == expected_status
        assert delivery.attempts == 1
        assert delivery.locked_until is None
        assert delivery.lease_token is None

        assert len(attempts) == 1
        assert attempts[0].status_code == status_code
        assert attempts[0].outcome == expected_outcome
        assert attempts[0].error_message is not None
        assert attempts[0].finished_at is not None

        if expected_status == "pending":
            available_at = self.utc_datetime(delivery.available_at)
            assert available_at > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_connection_error_schedules_retry(
        self,
        session_factory,
    ):
        delivery_id, _, _, lease_token = await self.create_delivery(session_factory)

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "connection refused",
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                lease_token,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.attempts == 1
        assert delivery.locked_until is None
        assert delivery.lease_token is None
        assert self.utc_datetime(delivery.available_at) > datetime.now(UTC)

        assert len(attempts) == 1
        assert attempts[0].status_code is None
        assert attempts[0].outcome == "failed"
        assert attempts[0].error_message == "Network request failed."
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_timeout_schedules_retry(
        self,
        session_factory,
    ):
        delivery_id, _, _, lease_token = await self.create_delivery(session_factory)

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(
                "receiver timed out",
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                lease_token,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.locked_until is None
        assert delivery.lease_token is None
        assert self.utc_datetime(delivery.available_at) > datetime.now(UTC)

        assert len(attempts) == 1
        assert attempts[0].status_code is None
        assert attempts[0].outcome == "failed"
        assert attempts[0].error_message == "Request timed out."
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_retry_exhaustion_marks_delivery_failed(
        self,
        session_factory,
    ):
        delivery_id, _, _, lease_token = await self.create_delivery(
            session_factory,
            attempts=DEFAULT_MAX_ATTEMPTS,
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                lease_token,
                max_attempts=DEFAULT_MAX_ATTEMPTS,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.attempts == DEFAULT_MAX_ATTEMPTS
        assert delivery.locked_until is None
        assert delivery.lease_token is None

        assert len(attempts) == DEFAULT_MAX_ATTEMPTS
        current_attempt = attempts[-1]
        assert current_attempt.attempt_number == DEFAULT_MAX_ATTEMPTS
        assert current_attempt.status_code == 503
        assert current_attempt.outcome == "failed"
        assert current_attempt.finished_at is not None

    @pytest.mark.asyncio
    async def test_non_processing_delivery_is_not_sent(
        self,
        session_factory,
    ):
        delivery_id, _, _, _ = await self.create_delivery(
            session_factory,
            status="pending",
            attempts=0,
        )
        request_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(204)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
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
        assert delivery.status == "pending"
        assert delivery.attempts == 0
        assert attempts == []

    @pytest.mark.asyncio
    async def test_stale_lease_token_cannot_send_request(
        self,
        session_factory,
    ):
        delivery_id, _, _, current_token = await self.create_delivery(session_factory)
        stale_token = uuid.uuid4()
        request_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(204)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                stale_token,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert current_token is not None
        assert request_count == 0
        assert delivery is not None
        assert delivery.status == "processing"
        assert delivery.lease_token == current_token
        assert len(attempts) == 1
        assert attempts[0].outcome == "processing"

    @pytest.mark.asyncio
    async def test_attempt_history_updates_existing_claimed_attempt(
        self,
        session_factory,
    ):
        delivery_id, _, _, lease_token = await self.create_delivery(
            session_factory,
            attempts=2,
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
                lease_token,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        # The attempt count was incremented during claiming.
        assert delivery.attempts == 2
        assert delivery.status == "succeeded"

        assert len(attempts) == 2
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert attempts[0].outcome == "failed"
        assert attempts[1].outcome == "succeeded"
        assert attempts[1].status_code == 204
        assert attempts[1].finished_at is not None


class TestClaimDeliveries(DeliveryTestHelpers):
    @pytest.mark.asyncio
    async def test_claim_increments_attempt_and_creates_history(
        self,
        session_factory,
    ):
        delivery_id, _, _, _ = await self.create_delivery(
            session_factory,
            status="pending",
            attempts=0,
        )

        claimed = await claim_deliveries(
            session_factory,
            batch_size=10,
            lease_seconds=60,
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
    async def test_future_delivery_is_not_claimed(
        self,
        session_factory,
    ):
        delivery_id, _, _, _ = await self.create_delivery(
            session_factory,
            status="pending",
            attempts=0,
            available_at=datetime.now(UTC) + timedelta(hours=1),
        )

        claimed = await claim_deliveries(session_factory)

        assert all(item.id != delivery_id for item in claimed)

        delivery = await self.load_delivery(session_factory, delivery_id)
        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.attempts == 0


class TestRecoverExpiredDeliveries(DeliveryTestHelpers):
    @pytest.mark.asyncio
    async def test_expired_lease_is_recovered_for_retry(
        self,
        session_factory,
    ):
        delivery_id, _, _, _ = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
            locked_until=datetime.now(UTC) - timedelta(seconds=5),
        )

        recovered_count = await recover_expired_deliveries(
            session_factory,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert recovered_count == 1
        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.locked_until is None
        assert delivery.lease_token is None
        assert self.utc_datetime(delivery.available_at) > datetime.now(UTC)

        assert len(attempts) == 1
        assert attempts[0].outcome == "abandoned"
        assert attempts[0].error_message is not None
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_expired_lease_at_attempt_limit_fails_delivery(
        self,
        session_factory,
    ):
        delivery_id, _, _, _ = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=DEFAULT_MAX_ATTEMPTS,
            locked_until=datetime.now(UTC) - timedelta(seconds=5),
        )

        recovered_count = await recover_expired_deliveries(
            session_factory,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
        )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert recovered_count == 1
        assert delivery is not None
        assert delivery.status == "failed"
        assert delivery.locked_until is None
        assert delivery.lease_token is None
        assert delivery.attempts == DEFAULT_MAX_ATTEMPTS

        assert len(attempts) == DEFAULT_MAX_ATTEMPTS
        assert attempts[-1].outcome == "abandoned"
        assert attempts[-1].finished_at is not None

    @pytest.mark.asyncio
    async def test_unexpired_lease_is_not_recovered(
        self,
        session_factory,
    ):
        delivery_id, _, _, lease_token = await self.create_delivery(
            session_factory,
            status="processing",
            attempts=1,
            locked_until=datetime.now(UTC) + timedelta(minutes=5),
        )

        recovered_count = await recover_expired_deliveries(
            session_factory,
        )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert recovered_count == 0
        assert delivery is not None
        assert delivery.status == "processing"
        assert delivery.lease_token == lease_token
        assert attempts[0].outcome == "processing"
        assert attempts[0].finished_at is None
