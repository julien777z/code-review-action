import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import uvicorn
from fastapi import FastAPI

from code_review_server.core.config import SERVER_SETTINGS
from code_review_server.routes import health_router, webhooks_router
from code_review_server.services.worker import review_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the single review worker for the app's lifetime and stop it on shutdown."""

    review_worker.start()

    try:
        yield
    finally:
        await review_worker.stop()


app: Final[FastAPI] = FastAPI(title="code-review-action backend", lifespan=lifespan)

for router in (health_router, webhooks_router):
    app.include_router(router)


def start_server() -> None:
    """Run the webhook backend with uvicorn on the configured host and port."""

    uvicorn.run(
        "code_review_server.runtime:app",
        host=SERVER_SETTINGS.host,
        port=SERVER_SETTINGS.port,
        log_level=SERVER_SETTINGS.log_level,
    )


def main() -> None:
    """Validate configuration and start the webhook backend."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not SERVER_SETTINGS.is_configured:
        logger.error("Set GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, and GITHUB_WEBHOOK_SECRET before starting.")

        sys.exit(1)

    start_server()
