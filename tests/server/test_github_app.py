import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import jwt

from code_review_server.github_app import build_app_jwt, mint_installation_token


class TestBuildAppJwt:
    """Test that the App JWT is signed with RS256 and carries the issuer and a short expiry."""

    def test_signs_a_verifiable_jwt(self, rsa_key_pair: tuple[str, str]) -> None:
        """Test that the JWT verifies with the App public key and carries the issuer and expiry."""

        private_pem, public_pem = rsa_key_pair

        token = build_app_jwt("123456", private_pem)
        claims = jwt.decode(token, public_pem, algorithms=["RS256"])

        assert claims["iss"] == "123456"
        assert claims["exp"] > claims["iat"]


class TestMintInstallationToken:
    """Test that an installation token is exchanged for the App JWT at the installation endpoint."""

    def test_posts_to_the_installation_and_returns_the_token(
        self, rsa_key_pair: tuple[str, str], mock_mint_response: Callable[..., AsyncMock]
    ) -> None:
        """Test that minting calls the installation access-tokens endpoint as the App and returns the token."""

        private_pem, _ = rsa_key_pair
        post = mock_mint_response("ghs_minted")

        token = asyncio.run(mint_installation_token("123456", private_pem, 42))

        assert token == "ghs_minted"

        post.assert_awaited_once()
        url = post.await_args.args[0]
        headers = post.await_args.kwargs["headers"]

        assert url.endswith("/app/installations/42/access_tokens")
        assert headers["Authorization"].startswith("Bearer ")
