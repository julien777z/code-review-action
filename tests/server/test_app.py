from collections.abc import Callable
from http import HTTPStatus
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from code_review_server import app as app_module
from code_review_server.app import app
from code_review_server.config import SERVER_SETTINGS


class TestHealth:
    """Test that the health endpoint reports liveness."""

    def test_health_ok(self) -> None:
        """Test that GET /health returns ok."""

        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"status": "ok"}


class TestGithubWebhook:
    """Test that the webhook verifies signatures and enqueues only eligible pull-request events."""

    def test_rejects_invalid_signature(self, monkeypatch, webhook_secret: str) -> None:
        """Test that a bad signature is rejected as unauthorized."""

        monkeypatch.setattr(SERVER_SETTINGS, "github_webhook_secret", webhook_secret)

        with TestClient(app) as client:
            response = client.post(
                "/webhooks/github",
                content=b"{}",
                headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=bad"},
            )

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_ignores_unsupported_event(
        self, monkeypatch, webhook_secret: str, sign_body: Callable[[bytes, str], str]
    ) -> None:
        """Test that a correctly-signed delivery of an unsupported event is ignored."""

        monkeypatch.setattr(SERVER_SETTINGS, "github_webhook_secret", webhook_secret)
        body = b"{}"

        with TestClient(app) as client:
            response = client.post(
                "/webhooks/github",
                content=body,
                headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sign_body(body, webhook_secret)},
            )

        assert response.status_code == HTTPStatus.NO_CONTENT

    def test_enqueues_eligible_pull_request(
        self,
        monkeypatch,
        webhook_secret: str,
        sign_body: Callable[[bytes, str], str],
        pull_request_webhook_factory: Callable[..., bytes],
    ) -> None:
        """Test that an eligible pull_request delivery is enqueued with its repo and installation."""

        monkeypatch.setattr(SERVER_SETTINGS, "github_webhook_secret", webhook_secret)
        submit = AsyncMock()
        monkeypatch.setattr(app_module.worker, "submit", submit)
        body = pull_request_webhook_factory()

        with TestClient(app) as client:
            response = client.post(
                "/webhooks/github",
                content=body,
                headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign_body(body, webhook_secret)},
            )

        assert response.status_code == HTTPStatus.ACCEPTED

        submit.assert_awaited_once()
        job = submit.await_args.args[0]

        assert job.repo == "octo/repo"
        assert job.installation_id == 42
