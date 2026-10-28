from collections.abc import Iterator

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


class TestLivenessEndpoint:
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


class TestReadinessEndpoint:
    def test_readiness_returns_success_when_database_is_available(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def database_is_available() -> bool:
            return True

        monkeypatch.setattr(
            "app.main.check_database_connection",
            database_is_available,
        )

        response = client.get("/health/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready"}

    def test_readiness_returns_service_unavailable_when_database_is_unavailable(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def database_is_unavailable() -> bool:
            return False

        monkeypatch.setattr(
            "app.main.check_database_connection",
            database_is_unavailable,
        )

        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "service_unavailable",
                "message": "Service temporarily unavailable.",
            }
        }
