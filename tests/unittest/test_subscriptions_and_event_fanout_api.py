import uuid

import pytest
import pytest_asyncio
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.delivery import Delivery
from app.models.endpoint_subscription import EndpointSubscription
from app.models.event import Event
from app.models.tenant import Tenant
from app.models.webhook_endpoint import WebhookEndpoint
from app.security.dependencies import get_current_tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def api_context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        tenant_a = Tenant(name="Tenant A", slug="tenant-a")
        tenant_b = Tenant(name="Tenant B", slug="tenant-b")
        session.add_all([tenant_a, tenant_b])
        await session.commit()
        await session.refresh(tenant_a)
        await session.refresh(tenant_b)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    previous_db = app.dependency_overrides.get(get_db)
    previous_tenant = app.dependency_overrides.get(get_current_tenant)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_tenant] = lambda: tenant_a

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield {
                "client": client,
                "engine": engine,
                "session_factory": session_factory,
                "tenant_a": tenant_a,
                "tenant_b": tenant_b,
            }
    finally:
        if previous_db is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_db
        if previous_tenant is None:
            app.dependency_overrides.pop(get_current_tenant, None)
        else:
            app.dependency_overrides[get_current_tenant] = previous_tenant
        await engine.dispose()


class TestSubscriptionAPI:
    @pytest.mark.asyncio
    async def test_creates_subscription_for_owned_endpoint(self, api_context):
        client = api_context["client"]
        tenant = api_context["tenant_a"]
        Session = api_context["session_factory"]

        async with Session() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant.id,
                url="https://example.com/hook",
                description=None,
                is_active=True,
            )
            session.add(endpoint)
            await session.commit()
            await session.refresh(endpoint)
            endpoint_id = endpoint.id

        response = await client.post(
            f"/api/v1/endpoints/{endpoint_id}/subscriptions",
            json={"event_type": "invoice.created"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["endpoint_id"] == str(endpoint_id)
        assert body["event_type"] == "invoice.created"
        assert body["id"]

        async with Session() as session:
            saved = (
                await session.execute(
                    select(EndpointSubscription).where(
                        EndpointSubscription.endpoint_id == endpoint_id
                    )
                )
            ).scalar_one()
            assert saved.event_type == "invoice.created"

    @pytest.mark.asyncio
    async def test_rejects_duplicate_subscription(self, api_context):
        client = api_context["client"]
        tenant = api_context["tenant_a"]
        Session = api_context["session_factory"]

        async with Session() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant.id,
                url="https://example.com/hook",
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
            await session.commit()
            endpoint_id = endpoint.id

        response = await client.post(
            f"/api/v1/endpoints/{endpoint_id}/subscriptions",
            json={"event_type": "invoice.created"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "subscription_exists"

    @pytest.mark.asyncio
    async def test_rejects_subscription_for_another_tenants_endpoint(self, api_context):
        client = api_context["client"]
        tenant_b = api_context["tenant_b"]
        Session = api_context["session_factory"]

        async with Session() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant_b.id,
                url="https://example.org/hook",
                is_active=True,
            )
            session.add(endpoint)
            await session.commit()
            endpoint_id = endpoint.id

        response = await client.post(
            f"/api/v1/endpoints/{endpoint_id}/subscriptions",
            json={"event_type": "invoice.created"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "endpoint_not_found"

    @pytest.mark.asyncio
    async def test_rejects_invalid_event_type(self, api_context):
        response = await api_context["client"].post(
            "/api/v1/endpoints/00000000-0000-0000-0000-000000000001/subscriptions",
            json={"event_type": ".invalid"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


class TestEventDeliveryFanout:
    @pytest.mark.asyncio
    async def test_creates_deliveries_only_for_active_matching_subscriptions(
        self, api_context
    ):
        client = api_context["client"]
        tenant_a = api_context["tenant_a"]
        tenant_b = api_context["tenant_b"]
        Session = api_context["session_factory"]

        async with Session() as session:
            matching_active = WebhookEndpoint(
                tenant_id=tenant_a.id,
                url="https://a.example.com/hook",
                is_active=True,
            )
            matching_inactive = WebhookEndpoint(
                tenant_id=tenant_a.id,
                url="https://b.example.com/hook",
                is_active=False,
            )
            nonmatching_active = WebhookEndpoint(
                tenant_id=tenant_a.id,
                url="https://c.example.com/hook",
                is_active=True,
            )
            other_tenant_active = WebhookEndpoint(
                tenant_id=tenant_b.id,
                url="https://d.example.com/hook",
                is_active=True,
            )

            session.add_all(
                [
                    matching_active,
                    matching_inactive,
                    nonmatching_active,
                    other_tenant_active,
                ]
            )
            await session.flush()

            session.add_all(
                [
                    EndpointSubscription(
                        endpoint_id=matching_active.id,
                        event_type="invoice.created",
                    ),
                    EndpointSubscription(
                        endpoint_id=matching_inactive.id,
                        event_type="invoice.created",
                    ),
                    EndpointSubscription(
                        endpoint_id=nonmatching_active.id,
                        event_type="invoice.updated",
                    ),
                    EndpointSubscription(
                        endpoint_id=other_tenant_active.id,
                        event_type="invoice.created",
                    ),
                ]
            )
            await session.commit()

            expected_endpoint_id = matching_active.id

        response = await client.post(
            "/api/v1/events",
            headers={"Idempotency-Key": "fanout-case-001"},
            json={
                "event_type": "invoice.created",
                "payload": {"invoice_id": "inv-123"},
            },
        )

        assert response.status_code == 201

        # JSON represents UUID values as strings.
        event_id = uuid.UUID(response.json()["id"])

        async with Session() as session:
            deliveries = (
                (
                    await session.execute(
                        select(Delivery).where(Delivery.event_id == event_id)
                    )
                )
                .scalars()
                .all()
            )

            assert len(deliveries) == 1
            assert deliveries[0].endpoint_id == expected_endpoint_id
            assert deliveries[0].status == "pending"
            assert deliveries[0].attempts == 0

            event_count = await session.scalar(
                select(func.count()).select_from(Event).where(Event.id == event_id)
            )

            assert event_count == 1

    @pytest.mark.asyncio
    async def test_patch_rejects_null_url_and_null_is_active(self, api_context):
        client = api_context["client"]
        tenant = api_context["tenant_a"]
        Session = api_context["session_factory"]

        async with Session() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant.id,
                url="https://example.com/hook",
                description="Keep me",
                is_active=True,
            )
            session.add(endpoint)
            await session.commit()
            endpoint_id = endpoint.id

        null_url = await client.patch(
            f"/api/v1/endpoints/{endpoint_id}", json={"url": None}
        )
        assert null_url.status_code == 422

        null_active = await client.patch(
            f"/api/v1/endpoints/{endpoint_id}", json={"is_active": None}
        )
        assert null_active.status_code == 422

        async with Session() as session:
            saved = await session.get(WebhookEndpoint, endpoint_id)
            assert saved is not None
            assert saved.url == "https://example.com/hook"
            assert saved.description == "Keep me"
            assert saved.is_active is True

    @pytest.mark.asyncio
    async def test_patch_can_clear_description(self, api_context):
        client = api_context["client"]
        tenant = api_context["tenant_a"]
        Session = api_context["session_factory"]

        async with Session() as session:
            endpoint = WebhookEndpoint(
                tenant_id=tenant.id,
                url="https://example.com/hook",
                description="Existing description",
                is_active=True,
            )
            session.add(endpoint)
            await session.commit()
            endpoint_id = endpoint.id

        response = await client.patch(
            f"/api/v1/endpoints/{endpoint_id}", json={"description": "   "}
        )
        assert response.status_code == 200
        assert response.json()["description"] is None

        async with Session() as session:
            saved = await session.get(WebhookEndpoint, endpoint_id)
            assert saved is not None
            assert saved.description is None
