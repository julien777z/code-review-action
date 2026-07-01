from collections.abc import Callable
from http import HTTPStatus
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from code_review_server.runtime import app
from code_review_server.services.webhooks import review_worker


class TestGithubWebhook:
    """Test that the webhook endpoint authenticates deliveries and enqueues eligible reviews."""

    def test_rejects_invalid_signature(self, mock_server_settings: Callable[..., None], webhook_secret: str) -> None:
        """Test that a bad signature is rejected as unauthorized."""

        mock_server_settings(github_webhook_secret=webhook_secret)

        with TestClient(app) as client:
            response = client.post(
                "/webhooks/github",
                content=b"{}",
                headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=bad"},
            )

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_enqueues_eligible_pull_request(
        self,
        monkeypatch,
        mock_server_settings: Callable[..., None],
        webhook_secret: str,
        sign_body: Callable[[bytes, str], str],
        pull_request_webhook_factory: Callable[..., bytes],
    ) -> None:
        """Test that an eligible pull_request delivery is accepted and enqueued with its repo and installation."""

        mock_server_settings(github_webhook_secret=webhook_secret)
        submit = AsyncMock()
        monkeypatch.setattr(review_worker, "submit", submit)
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
