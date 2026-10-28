import pytest
from app.db.session import get_db
from app.main import app
from fastapi.testclient import TestClient


class EmptyResult:
    def scalar_one_or_none(self):
        return None


class FakeSession:
    async def execute(self, statement):
        return EmptyResult()


@pytest.fixture
def auth_client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def invalid_key_client():
    async def override_get_db():
        yield FakeSession()

    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


class TestAuthenticationEndpoint:
    def test_missing_api_key_returns_401(
        self,
        auth_client: TestClient,
    ) -> None:
        response = auth_client.get("/api/v1/auth/me")

        assert response.status_code == 401
        assert response.json() == {
            "error": {
                "code": "unauthorized",
                "message": "Authentication required.",
            }
        }

    def test_unknown_api_key_returns_401(
        self,
        invalid_key_client: TestClient,
    ) -> None:
        response = invalid_key_client.get(
            "/api/v1/auth/me",
            headers={"X-API-Key": "hr_live_unknown-secret"},
        )

        assert response.status_code == 401
        assert response.json() == {
            "error": {
                "code": "unauthorized",
                "message": "Authentication required.",
            }
        }

    def test_api_key_header_is_documented(
        self,
        auth_client: TestClient,
    ) -> None:
        response = auth_client.get("/openapi.json")

        assert response.status_code == 200

        operation = response.json()["paths"]["/api/v1/auth/me"]["get"]

        assert any(
            parameter["name"] == "X-API-Key" and parameter["in"] == "header"
            for parameter in operation.get("parameters", [])
        )
