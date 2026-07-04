import logging
from datetime import datetime, timedelta, timezone
from typing import Final, TypedDict

import jwt

from code_review.utils.http import http_client
from code_review_server.core.config import SERVER_SETTINGS

logger = logging.getLogger(__name__)


class GithubAppConfig(TypedDict):
    """Static GitHub App integration values: REST host, API version, and JWT lifetime bounds."""

    api_host: str
    api_version: str
    jwt_ttl: timedelta
    jwt_clock_skew: timedelta


GITHUB_APP_CONFIG: Final[GithubAppConfig] = GithubAppConfig(
    api_host="https://api.github.com",
    api_version="2022-11-28",
    jwt_ttl=timedelta(minutes=9),
    jwt_clock_skew=timedelta(seconds=60),
)


def build_app_jwt() -> str:
    """Sign a short-lived GitHub App JWT (RS256) for authenticating as the App."""

    now = datetime.now(tz=timezone.utc)
    payload = {
        "iat": now - GITHUB_APP_CONFIG["jwt_clock_skew"],
        "exp": now + GITHUB_APP_CONFIG["jwt_ttl"],
        "iss": SERVER_SETTINGS.github_app_id,
    }

    return jwt.encode(payload, SERVER_SETTINGS.github_app_private_key, algorithm="RS256")


async def mint_installation_token(installation_id: int) -> str:
    """Exchange an App JWT for a short-lived installation access token scoped to the installation."""

    headers = {
        "Authorization": f"Bearer {build_app_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_APP_CONFIG["api_version"],
    }
    url = f"{GITHUB_APP_CONFIG['api_host']}/app/installations/{installation_id}/access_tokens"

    async with http_client() as client:
        response = await client.post(url, headers=headers)

    response.raise_for_status()

    return response.json()["token"]
