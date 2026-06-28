from collections.abc import Callable

import pytest

from code_review_server.webhook import verify_signature


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
