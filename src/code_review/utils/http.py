from datetime import timedelta
from typing import Final

import httpx

HTTP_TIMEOUT: Final[timedelta] = timedelta(seconds=30)


def http_client() -> httpx.AsyncClient:
    """Create an httpx async client with the shared timeout for outbound requests."""

    return httpx.AsyncClient(timeout=HTTP_TIMEOUT.total_seconds())
