from collections.abc import Iterator

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


class TestHealthEndpoint:
    def test_liveness_returns_successful_response(
        self,
        client: TestClient,
    ) -> None:
        response = client.get("/health/live")

        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
        assert response.headers["content-type"].startswith("application/json")

    def test_liveness_is_registered_in_openapi_schema(
        self,
        client: TestClient,
    ) -> None:
        response = client.get("/openapi.json")

        assert response.status_code == 200

        schema = response.json()
        assert "/health/live" in schema["paths"]
        assert "get" in schema["paths"]["/health/live"]

    def test_liveness_rejects_unsupported_http_method(
        self,
        client: TestClient,
    ) -> None:
        response = client.post("/health/live")

        assert response.status_code == 405
