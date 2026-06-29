import asyncio
from collections.abc import Callable
from http import HTTPStatus
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from code_review_server.services.webhooks import process_github_delivery, review_worker, verify_signature


class TestVerifySignature:
    """Test that the webhook HMAC signature is verified against the shared secret."""

    def test_accepts_a_valid_signature(self, webhook_secret: str, sign_body: Callable[[bytes, str], str]) -> None:
        """Test that a correct sha256 HMAC of the body is accepted."""

        body = b'{"zen": "ok"}'

        assert verify_signature(body, sign_body(body, webhook_secret), webhook_secret) is True

    def test_rejects_a_tampered_body(self, webhook_secret: str, sign_body: Callable[[bytes, str], str]) -> None:
        """Test that a signature computed over a different body is rejected."""

        signature = sign_body(b"original", webhook_secret)

        assert verify_signature(b"tampered", signature, webhook_secret) is False

    @pytest.mark.parametrize(
        "header",
        [None, "", "sha1=abc", "deadbeef"],
        ids=["missing", "empty", "wrong-algo", "no-prefix"],
    )
    def test_rejects_malformed_or_missing_header(self, webhook_secret: str, header: str | None) -> None:
        """Test that a missing or non-sha256 signature header is rejected."""

        assert verify_signature(b"body", header, webhook_secret) is False

    def test_fails_closed_without_a_secret(self, sign_body: Callable[[bytes, str], str]) -> None:
        """Test that verification fails when no secret is configured, even with a matching digest."""

        body = b"body"

        assert verify_signature(body, sign_body(body, ""), "") is False


class TestProcessGithubDelivery:
    """Test that delivery processing authenticates, filters, and enqueues eligible reviews under the App."""

    def test_rejects_invalid_signature_as_unauthorized(
        self, mock_server_settings: Callable[..., None], webhook_secret: str
    ) -> None:
        """Test that a delivery with a bad signature raises an unauthorized error."""

        mock_server_settings(github_webhook_secret=webhook_secret)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(process_github_delivery(b"{}", "sha256=bad", "pull_request"))

        assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED

    def test_ignores_unsupported_event_as_no_content(
        self,
        mock_server_settings: Callable[..., None],
        webhook_secret: str,
        sign_body: Callable[[bytes, str], str],
    ) -> None:
        """Test that a correctly-signed delivery of an unsupported event is ignored."""

        mock_server_settings(github_webhook_secret=webhook_secret)
        body = b"{}"

        status = asyncio.run(process_github_delivery(body, sign_body(body, webhook_secret), "push"))

        assert status == HTTPStatus.NO_CONTENT

    def test_rejects_missing_repo_or_installation_as_bad_request(
        self,
        mock_server_settings: Callable[..., None],
        webhook_secret: str,
        sign_body: Callable[[bytes, str], str],
    ) -> None:
        """Test that a supported delivery without repository or installation raises a bad-request error."""

        mock_server_settings(github_webhook_secret=webhook_secret)
        body = b'{"action": "opened"}'

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(process_github_delivery(body, sign_body(body, webhook_secret), "pull_request"))

        assert exc_info.value.status_code == HTTPStatus.BAD_REQUEST

    def test_ignores_ineligible_delivery_as_no_content(
        self,
        mock_server_settings: Callable[..., None],
        webhook_secret: str,
        sign_body: Callable[[bytes, str], str],
        pull_request_webhook_factory: Callable[..., bytes],
    ) -> None:
        """Test that an otherwise-valid delivery from a bot actor is ignored."""

        mock_server_settings(github_webhook_secret=webhook_secret)
        body = pull_request_webhook_factory(sender={"type": "Bot"})

        status = asyncio.run(process_github_delivery(body, sign_body(body, webhook_secret), "pull_request"))

        assert status == HTTPStatus.NO_CONTENT

    def test_accepts_and_enqueues_eligible_delivery(
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

        status = asyncio.run(process_github_delivery(body, sign_body(body, webhook_secret), "pull_request"))

        assert status == HTTPStatus.ACCEPTED

        submit.assert_awaited_once()
        job = submit.await_args.args[0]

        assert job.repo == "octo/repo"
        assert job.installation_id == 42
