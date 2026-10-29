from collections.abc import Iterator

import pytest
from app.errors import register_exception_handlers
from app.schemas import TenantCreate
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def error_test_client() -> Iterator[TestClient]:
    test_app = FastAPI()
    register_exception_handlers(test_app)

    @test_app.post("/schema-probe")
    async def schema_probe(payload: TenantCreate) -> dict[str, str]:
        return {
            "name": payload.name,
            "slug": payload.slug,
        }

    with TestClient(test_app) as client:
        yield client


class TestRequestValidationErrors:
    def test_invalid_request_returns_standardized_validation_error(
        self,
        error_test_client: TestClient,
    ) -> None:
        response = error_test_client.post(
            "/schema-probe",
            json={
                "name": "Example Organization",
                "slug": "Invalid Slug",
            },
        )

        assert response.status_code == 422

        body = response.json()
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["message"] == "Request validation failed."

        details = body["error"]["details"]
        assert any(
            detail["location"] == ["body", "slug"]
            and detail["type"] == "string_pattern_mismatch"
            for detail in details
        )

    def test_invalid_request_does_not_echo_input_values(
        self,
        error_test_client: TestClient,
    ) -> None:
        secret_value = "Sensitive Invalid Secret"

        response = error_test_client.post(
            "/schema-probe",
            json={
                "name": "Example Organization",
                "slug": secret_value,
            },
        )

        assert response.status_code == 422
        assert secret_value not in response.text


class TestHttpErrors:
    def test_unknown_route_returns_standardized_not_found_error(
        self,
        error_test_client: TestClient,
    ) -> None:
        response = error_test_client.get("/does-not-exist")

        assert response.status_code == 404
        assert response.json() == {
            "error": {
                "code": "not_found",
                "message": "Resource not found.",
            }
        }
