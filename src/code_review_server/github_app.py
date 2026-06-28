import logging
from datetime import datetime, timedelta, timezone
from typing import Final

import httpx
import jwt

logger = logging.getLogger("code_review_server.github_app")

GITHUB_API_HOST: Final[str] = "https://api.github.com"
GITHUB_API_VERSION: Final[str] = "2022-11-28"
APP_JWT_TTL: Final[timedelta] = timedelta(minutes=9)
APP_JWT_CLOCK_SKEW: Final[timedelta] = timedelta(seconds=60)
MINT_TIMEOUT_SECONDS: Final[float] = 30.0


def build_app_jwt(app_id: str, private_key: str) -> str:
    """Sign a short-lived GitHub App JWT (RS256) for authenticating as the App."""

    now = datetime.now(tz=timezone.utc)
    payload = {"iat": now - APP_JWT_CLOCK_SKEW, "exp": now + APP_JWT_TTL, "iss": app_id}

    return jwt.encode(payload, private_key, algorithm="RS256")


async def mint_installation_token(app_id: str, private_key: str, installation_id: int) -> str:
    """Exchange an App JWT for a short-lived installation access token scoped to the installation."""

    headers = {
        "Authorization": f"Bearer {build_app_jwt(app_id, private_key)}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    url = f"{GITHUB_API_HOST}/app/installations/{installation_id}/access_tokens"

    async with httpx.AsyncClient(timeout=MINT_TIMEOUT_SECONDS) as client:
        response = await client.post(url, headers=headers)

    response.raise_for_status()

    return response.json()["token"]
