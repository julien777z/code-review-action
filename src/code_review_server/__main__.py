import logging
import sys

import uvicorn

from code_review_server.config import SERVER_SETTINGS

logger = logging.getLogger("code_review_server")


def main() -> None:
    """Validate configuration and run the webhook backend with uvicorn."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not SERVER_SETTINGS.is_configured:
        logger.error("Set GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, and GITHUB_WEBHOOK_SECRET before starting.")

        sys.exit(1)

    uvicorn.run(
        "code_review_server.app:app",
        host=SERVER_SETTINGS.host,
        port=SERVER_SETTINGS.port,
        log_level=SERVER_SETTINGS.log_level,
    )


if __name__ == "__main__":
    main()
