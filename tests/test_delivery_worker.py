import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from app.db.base import Base
from app.models import Delivery, DeliveryAttempt, Event, Tenant, WebhookEndpoint
from app.worker.delivery import execute_delivery
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

    yield factory

    await engine.dispose()


class TestExecuteDelivery:
    async def create_delivery(
        self,
        session_factory,
        *,
        status: str = "processing",
        attempts: int = 0,
    ) -> tuple[uuid.UUID, uuid.UUID, str]:
        """Create the minimum database records needed for delivery."""
        tenant_id = uuid.uuid4()
        endpoint_id = uuid.uuid4()
        event_id = uuid.uuid4()
        delivery_id = uuid.uuid4()

        async with session_factory() as session:
            tenant = Tenant(
                id=tenant_id,
                name="Test tenant",
                slug=f"tenant-{tenant_id.hex[:12]}",
            )
            endpoint = WebhookEndpoint(
                id=endpoint_id,
                tenant_id=tenant_id,
                url="https://receiver.example/webhooks",
                description="Test receiver",
                is_active=True,
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
                available_at=datetime.now(UTC),
            )

            session.add_all([tenant, endpoint, event, delivery])
            await session.commit()

        return delivery_id, event_id, endpoint.url

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

    @pytest.mark.asyncio
    async def test_successful_response_marks_delivery_succeeded(self, session_factory):
        delivery_id, event_id, endpoint_url = await self.create_delivery(
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
        assert delivery.locked_until is None
        assert delivery.attempts == 1

        assert len(attempts) == 1
        assert attempts[0].status_code == 204
        assert attempts[0].finished_at is not None
        assert attempts[0].error_message is None
        assert attempts[0].outcome

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 429, 500, 503])
    async def test_non_success_http_response_is_recorded_as_failure(
        self,
        session_factory,
        status_code,
    ):
        delivery_id, _, _ = await self.create_delivery(session_factory)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code,
                text="receiver error",
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.locked_until is None
        assert delivery.attempts == 1

        assert len(attempts) == 1
        assert attempts[0].status_code == status_code
        assert attempts[0].outcome
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_connection_error_is_recorded_without_crashing_worker(
        self, session_factory
    ):
        delivery_id, _, _ = await self.create_delivery(session_factory)

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "connection refused",
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.locked_until is None
        assert delivery.attempts == 1

        assert len(attempts) == 1
        assert attempts[0].status_code is None
        assert attempts[0].outcome
        assert attempts[0].error_message
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_timeout_is_recorded_as_failed_attempt(self, session_factory):
        delivery_id, _, _ = await self.create_delivery(session_factory)

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout(
                "receiver timed out",
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.attempts == 1
        assert len(attempts) == 1
        assert attempts[0].status_code is None
        assert attempts[0].error_message
        assert attempts[0].finished_at is not None

    @pytest.mark.asyncio
    async def test_delivery_not_in_processing_state_is_not_sent(self, session_factory):
        delivery_id, _, _ = await self.create_delivery(
            session_factory,
            status="pending",
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
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert request_count == 0
        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.attempts == 0
        assert attempts == []

    @pytest.mark.asyncio
    async def test_attempt_history_is_appended_on_each_execution(self, session_factory):
        delivery_id, _, _ = await self.create_delivery(
            session_factory,
            attempts=2,
        )

        # The worker is expected to create one additional attempt.
        # This test assumes the stored attempts count represents
        # completed delivery attempts.
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            await execute_delivery(
                session_factory,
                client,
                delivery_id,
            )

        delivery = await self.load_delivery(session_factory, delivery_id)
        attempts = await self.load_attempts(session_factory, delivery_id)

        assert delivery is not None
        assert delivery.attempts == 3
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 3
