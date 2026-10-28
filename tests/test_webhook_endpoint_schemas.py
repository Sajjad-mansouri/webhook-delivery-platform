import pytest
from app.schemas.webhook_endpoint import EndpointCreate, EndpointUpdate
from pydantic import ValidationError


class TestEndpointCreateSchema:
    def test_accepts_public_https_url(self) -> None:
        endpoint = EndpointCreate(
            url="https://hooks.example.com/events",
            description="Order events",
        )

        assert str(endpoint.url) == "https://hooks.example.com/events"
        assert endpoint.description == "Order events"

    def test_description_is_optional(self) -> None:
        endpoint = EndpointCreate(
            url="https://hooks.example.com/events",
        )

        assert endpoint.description is None

    def test_normalizes_blank_description_to_none(self) -> None:
        endpoint = EndpointCreate(
            url="https://hooks.example.com/events",
            description="   ",
        )

        assert endpoint.description is None

    @pytest.mark.parametrize(
        "url",
        [
            "http://hooks.example.com/events",
            "https://localhost/events",
            "https://service.local/events",
            "https://service.internal/events",
            "https://127.0.0.1/events",
            "https://192.168.1.10/events",
            "https://user:password@hooks.example.com/events",
            "https://hooks.example.com/events#fragment",
        ],
    )
    def test_rejects_disallowed_urls(self, url: str) -> None:
        with pytest.raises(ValidationError):
            EndpointCreate(url=url)

    def test_rejects_unexpected_tenant_id(self) -> None:
        with pytest.raises(ValidationError):
            EndpointCreate.model_validate(
                {
                    "url": "https://hooks.example.com/events",
                    "tenant_id": "another-tenant",
                }
            )

    def test_rejects_description_longer_than_limit(self) -> None:
        with pytest.raises(ValidationError):
            EndpointCreate(
                url="https://hooks.example.com/events",
                description="x" * 201,
            )


class TestEndpointUpdateSchema:
    def test_allows_partial_update(self) -> None:
        update = EndpointUpdate(is_active=False)

        assert update.model_dump(exclude_unset=True) == {
            "is_active": False,
        }

    def test_rejects_http_url(self) -> None:
        with pytest.raises(ValidationError):
            EndpointUpdate(url="http://hooks.example.com/events")
