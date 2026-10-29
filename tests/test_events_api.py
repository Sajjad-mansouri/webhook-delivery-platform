import uuid

import pytest
import pytest_asyncio
from app import models  # noqa: F401
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.api_key import APIKey
from app.models.event import Event
from app.models.tenant import Tenant
from app.security.api_keys import generate_api_key, hash_api_key
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)


@pytest_asyncio.fixture
async def event_api():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    tenant_a_id = uuid.uuid4()
    tenant_b_id = uuid.uuid4()
    raw_key_a = generate_api_key()
    raw_key_b = generate_api_key()

    async with session_factory.begin() as db:
        db.add_all(
            [
                Tenant(
                    id=tenant_a_id,
                    name="Tenant A",
                    slug="tenant-a",
                ),
                Tenant(
                    id=tenant_b_id,
                    name="Tenant B",
                    slug="tenant-b",
                ),
            ]
        )

        db.add_all(
            [
                APIKey(
                    tenant_id=tenant_a_id,
                    name="Test key A",
                    key_prefix=raw_key_a[:16],
                    key_hash=hash_api_key(raw_key_a),
                ),
                APIKey(
                    tenant_id=tenant_b_id,
                    name="Test key B",
                    key_prefix=raw_key_b[:16],
                    key_hash=hash_api_key(raw_key_b),
                ),
            ]
        )

    async def override_get_db():
        async with session_factory() as db:
            yield db

    fastapi_app.dependency_overrides[get_db] = override_get_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test",
        ) as client:
            yield {
                "client": client,
                "session_factory": session_factory,
                "tenant_a_id": tenant_a_id,
                "tenant_b_id": tenant_b_id,
                "key_a": raw_key_a,
                "key_b": raw_key_b,
            }
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


class TestEventAPI:
    @pytest.mark.asyncio
    async def test_create_event_assigns_authenticated_tenant(
        self,
        event_api,
    ):
        response = await event_api["client"].post(
            "/api/v1/events",
            headers={
                "X-API-Key": event_api["key_a"],
                "Idempotency-Key": "order-8472-created",
            },
            json={
                "event_type": "order.created",
                "payload": {"order_id": 8472},
            },
        )

        assert response.status_code == 201
        data = response.json()

        assert data["tenant_id"] == str(event_api["tenant_a_id"])
        assert data["event_type"] == "order.created"
        assert data["payload"] == {"order_id": 8472}
        assert data["idempotency_key"] == "order-8472-created"

    @pytest.mark.asyncio
    async def test_same_key_and_payload_return_existing_event(
        self,
        event_api,
    ):
        headers = {
            "X-API-Key": event_api["key_a"],
            "Idempotency-Key": "order-8472-created",
        }
        payload = {
            "event_type": "order.created",
            "payload": {"order_id": 8472},
        }

        first = await event_api["client"].post(
            "/api/v1/events",
            headers=headers,
            json=payload,
        )
        second = await event_api["client"].post(
            "/api/v1/events",
            headers=headers,
            json=payload,
        )

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]

        async with event_api["session_factory"]() as db:
            result = await db.execute(select(Event))
            events = result.scalars().all()

        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_reusing_key_with_different_payload_returns_conflict(
        self,
        event_api,
    ):
        headers = {
            "X-API-Key": event_api["key_a"],
            "Idempotency-Key": "order-8472-created",
        }

        first = await event_api["client"].post(
            "/api/v1/events",
            headers=headers,
            json={
                "event_type": "order.created",
                "payload": {"order_id": 8472},
            },
        )
        second = await event_api["client"].post(
            "/api/v1/events",
            headers=headers,
            json={
                "event_type": "order.cancelled",
                "payload": {"order_id": 8472},
            },
        )

        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "idempotency_key_conflict"

    @pytest.mark.asyncio
    async def test_tenants_can_use_the_same_idempotency_key(
        self,
        event_api,
    ):
        first = await event_api["client"].post(
            "/api/v1/events",
            headers={
                "X-API-Key": event_api["key_a"],
                "Idempotency-Key": "shared-key",
            },
            json={
                "event_type": "order.created",
                "payload": {"order_id": 1},
            },
        )
        second = await event_api["client"].post(
            "/api/v1/events",
            headers={
                "X-API-Key": event_api["key_b"],
                "Idempotency-Key": "shared-key",
            },
            json={
                "event_type": "order.created",
                "payload": {"order_id": 1},
            },
        )

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] != second.json()["id"]
        assert second.json()["tenant_id"] == str(event_api["tenant_b_id"])

    @pytest.mark.asyncio
    async def test_missing_api_key_cannot_create_event(self, event_api):
        response = await event_api["client"].post(
            "/api/v1/events",
            headers={"Idempotency-Key": "order-8472-created"},
            json={
                "event_type": "order.created",
                "payload": {"order_id": 8472},
            },
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_idempotency_key_is_rejected(
        self,
        event_api,
    ):
        response = await event_api["client"].post(
            "/api/v1/events",
            headers={"X-API-Key": event_api["key_a"]},
            json={
                "event_type": "order.created",
                "payload": {"order_id": 8472},
            },
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_client_cannot_supply_tenant_id(
        self,
        event_api,
    ):
        response = await event_api["client"].post(
            "/api/v1/events",
            headers={
                "X-API-Key": event_api["key_a"],
                "Idempotency-Key": "order-8472-created",
            },
            json={
                "tenant_id": str(event_api["tenant_b_id"]),
                "event_type": "order.created",
                "payload": {"order_id": 8472},
            },
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_event_type_is_rejected(
        self,
        event_api,
    ):
        response = await event_api["client"].post(
            "/api/v1/events",
            headers={
                "X-API-Key": event_api["key_a"],
                "Idempotency-Key": "invalid-event",
            },
            json={
                "event_type": "order created!",
                "payload": {"order_id": 8472},
            },
        )

        assert response.status_code == 422
