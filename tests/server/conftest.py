import hashlib
import hmac
import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from code_review_server import github_app


@pytest.fixture(scope="session")
def rsa_key_pair() -> tuple[str, str]:
    """Build a PEM RSA private/public key pair for signing and verifying App JWTs."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )

    return private_pem, public_pem


@pytest.fixture
def webhook_secret() -> str:
    """Return a fixed webhook secret for signature tests."""

    return "test-webhook-secret"


@pytest.fixture
def sign_body() -> Callable[[bytes, str], str]:
    """Build the X-Hub-Signature-256 header value for a raw body and secret."""

    def _sign(body: bytes, secret: str) -> str:
        return f"sha256={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"

    return _sign


@pytest.fixture
def pull_request_webhook_factory() -> Callable[..., bytes]:
    """Build a serialized pull_request webhook payload eligible for review."""

    def _build(**overrides: object) -> bytes:
        payload: dict[str, object] = {
            "action": "opened",
            "repository": {"full_name": "octo/repo"},
            "installation": {"id": 42},
            "sender": {"type": "User"},
            "pull_request": {
                "number": 7,
                "head": {"repo": {"full_name": "octo/repo"}, "sha": "abc123", "ref": "feature"},
                "author_association": "MEMBER",
                "draft": False,
            },
        }
        payload.update(overrides)

        return json.dumps(payload).encode()

    return _build


@pytest.fixture
def mock_mint_response(monkeypatch) -> Callable[..., AsyncMock]:
    """Patch the github_app httpx client to capture the mint request and return a canned token."""

    def _build(token: str = "ghs_minted") -> AsyncMock:
        response = MagicMock()
        response.json.return_value = {"token": token}
        response.raise_for_status.return_value = None

        client = MagicMock()
        client.post = AsyncMock(return_value=response)

        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(github_app.httpx, "AsyncClient", MagicMock(return_value=context))

        return client.post

    return _build
