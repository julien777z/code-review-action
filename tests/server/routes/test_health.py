from http import HTTPStatus

from fastapi.testclient import TestClient

from code_review_server.runtime import app


class TestHealth:
    """Test that the health endpoint reports liveness."""

    def test_returns_ok(self) -> None:
        """Test that GET /health returns ok."""

        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"status": "ok"}
