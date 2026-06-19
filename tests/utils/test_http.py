import asyncio

import httpx

from code_review.utils.http import HTTP_TIMEOUT, http_client


class TestHttpClient:
    """Test that the shared HTTP client factory applies the configured timeout."""

    def test_applies_shared_timeout(self) -> None:
        """Test that the factory returns an httpx async client carrying the shared timeout."""

        client = http_client()

        try:
            assert isinstance(client, httpx.AsyncClient)
            assert client.timeout == httpx.Timeout(HTTP_TIMEOUT.total_seconds())
        finally:
            asyncio.run(client.aclose())
