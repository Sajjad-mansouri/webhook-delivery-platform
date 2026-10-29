"""Integration test for the complete HookRelay delivery lifecycle.

SQLite runs by default. PostgreSQL runs when
HOOKRELAY_TEST_POSTGRES_URL points to a dedicated test database.

WARNING: This test drops and recreates tables in the selected database.
Never point HOOKRELAY_TEST_POSTGRES_URL at a development or production DB.
"""

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from app import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.delivery import Delivery
from app.models.delivery_attempt import DeliveryAttempt
from app.models.endpoint_subscription import EndpointSubscription
from app.models.event import Event
from app.models.tenant import Tenant
from app.models.webhook_endpoint import WebhookEndpoint
from app.security.webhook_signatures import encrypt_signing_secret
from app.services.api_keys import create_api_key
from app.worker.delivery import execute_delivery
from app.worker.queue import claim_deliveries
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

POSTGRES_URL_ENV = "HOOKRELAY_TEST_POSTGRES_URL"
TEST_SIGNING_SECRET = "integration-test-signing-secret"


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> str:
    """Run with SQLite and optionally PostgreSQL."""
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / 'hookrelay-integration.db'}"

    postgres_url = os.getenv(POSTGRES_URL_ENV)

    if not postgres_url:
        pytest.skip(f"Set {POSTGRES_URL_ENV} to run against PostgreSQL.")

    if not postgres_url.startswith("postgresql+asyncpg://"):
        pytest.fail(
            f"{POSTGRES_URL_ENV} must use an async SQLAlchemy URL, "
            "such as postgresql+asyncpg://user:password@localhost/hookrelay_test."
        )

    return postgres_url


@pytest_asyncio.fixture
async def integration_context(
    database_url: str,
) -> AsyncIterator[dict[str, Any]]:
    """Create a fresh database and an ASGI client using real API-key auth."""
    engine = create_async_engine(database_url)

    async with engine.begin() as connection:
        # The PostgreSQL URL MUST point to a disposable test database.
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    previous_overrides = dict(fastapi_app.dependency_overrides)

    try:
        async with session_factory.begin() as session:
            tenant = Tenant(
                name="Integration Tenant",
                slug="integration-tenant",
            )
            session.add(tenant)
            await session.flush()

            api_key_record, raw_api_key = await create_api_key(
                session,
                tenant_id=tenant.id,
                name="Integration test key",
            )

            endpoint = WebhookEndpoint(
                tenant_id=tenant.id,
                url="https://receiver.example.test/webhook",
                description="Integration test receiver",
                is_active=True,
                signing_secret_encrypted=encrypt_signing_secret(TEST_SIGNING_SECRET),
            )
            session.add(endpoint)
            await session.flush()

            session.add(
                EndpointSubscription(
                    endpoint_id=endpoint.id,
                    event_type="invoice.created",
                )
            )

            tenant_id = tenant.id
            endpoint_id = endpoint.id
            api_key_id = api_key_record.id

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            async with session_factory() as session:
                yield session

        # Override only the database dependency. Keep the real
        # get_current_tenant dependency so API-key authentication is tested.
        fastapi_app.dependency_overrides[get_db] = override_get_db

        transport = httpx.ASGITransport(app=fastapi_app)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield {
                "client": client,
                "session_factory": session_factory,
                "tenant_id": tenant_id,
                "endpoint_id": endpoint_id,
                "api_key_id": api_key_id,
                "api_key": raw_api_key,
            }

    finally:
        fastapi_app.dependency_overrides.clear()
        fastapi_app.dependency_overrides.update(previous_overrides)
        await engine.dispose()


class TestEventToDeliveryLifecycle:
    @pytest.mark.asyncio
    async def test_event_is_ingested_claimed_sent_and_persisted(
        self,
        integration_context: dict[str, Any],
    ) -> None:
        client: httpx.AsyncClient = integration_context["client"]
        session_factory = integration_context["session_factory"]
        tenant_id = integration_context["tenant_id"]
        endpoint_id = integration_context["endpoint_id"]
        api_key = integration_context["api_key"]

        event_payload = {
            "invoice_id": "inv-integration-001",
            "amount": 2500,
        }
        idempotency_key = f"integration-{uuid.uuid4()}"

        # --------------------------------------------------------------
        # 1. Ingest an event through the actual FastAPI route.
        # --------------------------------------------------------------
        response = await client.post(
            "/api/v1/events",
            headers={
                "X-API-Key": api_key,
                "Idempotency-Key": idempotency_key,
            },
            json={
                "event_type": "invoice.created",
                "payload": event_payload,
            },
        )

        assert response.status_code == 201, response.text

        response_data = response.json()
        event_id = uuid.UUID(response_data["id"])

        assert response_data["event_type"] == "invoice.created"
        assert response_data["payload"] == event_payload

        # --------------------------------------------------------------
        # 2. Verify persisted event and fan-out delivery.
        # --------------------------------------------------------------
        async with session_factory() as session:
            persisted_event = await session.get(Event, event_id)

            assert persisted_event is not None
            assert persisted_event.tenant_id == tenant_id
            assert persisted_event.idempotency_key == idempotency_key
            assert persisted_event.event_type == "invoice.created"
            assert persisted_event.payload == event_payload

            result = await session.execute(
                select(Delivery).where(Delivery.event_id == event_id)
            )
            deliveries = list(result.scalars().all())

            assert len(deliveries) == 1

            delivery = deliveries[0]
            assert delivery.endpoint_id == endpoint_id
            assert delivery.status == "pending"
            assert delivery.attempts == 0

            delivery_id = delivery.id

            event_count = await session.scalar(
                select(func.count()).select_from(Event).where(Event.id == event_id)
            )
            assert event_count == 1

        # --------------------------------------------------------------
        # 3. Claim the delivery through the real queue implementation.
        # --------------------------------------------------------------
        claimed = await claim_deliveries(
            session_factory,
            batch_size=10,
            lease_seconds=60,
        )

        matching_claims = [item for item in claimed if item.id == delivery_id]
        assert len(matching_claims) == 1

        claimed_delivery = matching_claims[0]

        assert claimed_delivery.status == "processing"
        assert claimed_delivery.attempts == 1
        assert claimed_delivery.lease_token is not None

        lease_token = claimed_delivery.lease_token

        async with session_factory() as session:
            result = await session.execute(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.delivery_id == delivery_id,
                    DeliveryAttempt.attempt_number == 1,
                )
            )
            claimed_attempt = result.scalar_one()

            assert claimed_attempt.outcome == "processing"
            assert claimed_attempt.finished_at is None

        # --------------------------------------------------------------
        # 4. Execute the actual worker. Mock only the external receiver.
        # --------------------------------------------------------------
        received: dict[str, Any] = {}

        async def receiver(
            request: httpx.Request,
        ) -> httpx.Response:
            received["method"] = request.method
            received["url"] = str(request.url)
            received["headers"] = dict(request.headers)
            received["body_bytes"] = request.content
            received["body"] = json.loads(request.content)

            return httpx.Response(204)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(receiver),
            follow_redirects=False,
        ) as receiver_client:
            await execute_delivery(
                session_factory,
                receiver_client,
                delivery_id,
                lease_token,
            )

        # --------------------------------------------------------------
        # 5. Verify the HTTP request and independently validate its HMAC.
        # --------------------------------------------------------------
        assert received["method"] == "POST"
        assert received["url"] == "https://receiver.example.test/webhook"

        headers = received["headers"]

        assert headers["content-type"].startswith("application/json")
        assert headers["x-hookrelay-event-id"] == str(event_id)

        assert received["body"] == {
            "id": str(event_id),
            "event_type": "invoice.created",
            "payload": event_payload,
        }

        timestamp = headers["x-hookplatfom-timestamp"]
        signature = headers["x-hookplatfom-signature"]

        assert timestamp.isdigit()
        assert signature.startswith("v1=")

        # The worker signs: timestamp + b"." + exact request body.
        signed_content = timestamp.encode("ascii") + b"." + received["body_bytes"]

        expected_digest = hmac.new(
            TEST_SIGNING_SECRET.encode("utf-8"),
            signed_content,
            hashlib.sha256,
        ).hexdigest()

        assert hmac.compare_digest(
            signature,
            f"v1={expected_digest}",
        )

        # --------------------------------------------------------------
        # 6. Verify final state using a new database session.
        # --------------------------------------------------------------
        async with session_factory() as session:
            final_delivery = await session.get(Delivery, delivery_id)

            assert final_delivery is not None
            assert final_delivery.status == "succeeded"
            assert final_delivery.attempts == 1
            assert final_delivery.locked_until is None
            assert final_delivery.lease_token is None

            result = await session.execute(
                select(DeliveryAttempt)
                .where(DeliveryAttempt.delivery_id == delivery_id)
                .order_by(DeliveryAttempt.attempt_number)
            )
            attempts = list(result.scalars().all())

            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome == "succeeded"
            assert attempts[0].status_code == 204
            assert attempts[0].error_message is None
            assert attempts[0].finished_at is not None
